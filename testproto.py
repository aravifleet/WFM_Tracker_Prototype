import os
import pyautogui
import threading
import time
import schedule
import sys
import sqlite3 
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import messagebox, simpledialog, Toplevel, StringVar, ttk
from functools import partial
from typing import Optional, Union 

# --- Configuration ---
LOG_DB_FILE = "prototype.db" # Database file

# --- Constants ---
IDLE_TIMEOUT_SECONDS = 600       # 10 minutes (600 seconds)
IDLE_CAUTION_DELAY = 10          # Seconds to wait after IDLE_TIMEOUT before showing caution
BREAK_DURATION_MINUTES = 15
LUNCH_DURATION_MINUTES = 30
OFF_WORK_LIMIT_HOURS = 8         # 8 hours limit for Off work timer
BREAK_EXCEED_LOG_INTERVAL = 600  # Log lunch/break exceed every 10 minutes (600 seconds)
BREAK_EXCEED_BUFFER_MINUTES = 30 # 30 minutes grace after break/lunch exceed before a simple reminder
BLINK_THRESHOLD_SECONDS = 15     # Start blinking when remaining time is under 15 seconds
ALLOWED_INTERVALS_INT = [15, 30, 60]
ALLOWED_INTERVALS = [str(i) for i in ALLOWED_INTERVALS_INT] # List of strings for Tkinter
STATUS_OPTIONS = ["Working", "Lunch", "Meeting", "Personal", "Break", "Offline", "Off work"]
DEFAULT_TASK_LABEL = "Hey, what are you doing right now?" 

# --- Global State (Type Hinted for Pylance) ---
last_response_time: list[datetime] = [datetime.now()]
current_status: list[str] = ["Working"] 
activity_thread: Optional[threading.Thread] = None
break_exceeded_flag = False
idle_check_flag = False
break_check_start_time: Optional[datetime] = None
POPUP_INTERVAL_MINUTES = 0 # This will hold the user-selected interval (e.g., 30)
SCHEDULER_LOCKED = False
USER_EMP_ID: list[str | None] = [None]
LAST_EXCEED_LOG_TIME: list[datetime | None] = [None]
OFF_WORK_START_TIME: list[datetime | None] = [None]
TIMED_STATUS_END_TIME: list[datetime | None] = [None] # Store end time for timed statuses (for countdown)
TIMER_VISIBLE = True # For blinking logic
WORK_REMAINING_SECONDS: list[int] = [0] # Stores remaining seconds of the work interval

# --- Database & Logging Functions ---

def init_db():
    """Initializes the SQLite database and creates the activity log table."""
    try:
        conn = sqlite3.connect(LOG_DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
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
        print(f"✅ Database initialized: {LOG_DB_FILE}")
    except Exception as e:
        print(f"⚠️ Warning: Failed to initialize database: {e}")


def log_to_db(response, status, remark="", log_time=None):
    """Inserts the status update into the SQLite database."""
    timestamp = log_time if log_time else datetime.now()
    emp_id = USER_EMP_ID[0] if USER_EMP_ID[0] else "N/A"
    
    try:
        conn = sqlite3.connect(LOG_DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO activity_log (timestamp, emp_id, status, response, remark)
            VALUES (?, ?, ?, ?, ?)
        ''', (timestamp.strftime('%Y-%m-%d %H:%M:%S'), emp_id, status, response, remark))
        conn.commit()
        conn.close()
        
        print(f"[{timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ✅ LOGGED to DB: {response} | Status: {status} | Remark: {remark} | ID: {emp_id}")
    except Exception as e:
        print(f"⚠️ Warning: Failed to log to database: {e}")


def get_last_status():
    """Reads the last status from the database."""
    try:
        if not os.path.exists(LOG_DB_FILE):
            return None
            
        conn = sqlite3.connect(LOG_DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT status FROM activity_log
            ORDER BY timestamp DESC
            LIMIT 1
        ''')
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else None
    except Exception as e:
        print(f"Error fetching last status from DB: {e}")
    return None


def check_and_log_unexpected_exit():
    """Logs Idle entry if user failed to log out last session."""
    last_status = get_last_status()
    if last_status and last_status not in ["Offline", "Off work", "Idle"]:
        timestamp = datetime.now()
        log_to_db(
            "Unexpected System Exit",
            "Idle",
            f"User failed to log Offline. Logged as IDLE on startup. Previous status: {last_status}",
            log_time=timestamp,
        )
        print("🛑 ALERT: Logged unexpected system exit.")
        return True
    return False


# --- Tkinter Application ---
class LoginApp:
    def __init__(self, master: tk.Tk):
        self.master = master
        master.title("Tracker Login")
        master.geometry("300x150")
        master.attributes('-topmost', True) 

        tk.Label(master, text="Employee ID (Mandatory):").pack(pady=5)
        self.emp_id_entry = tk.Entry(master, width=20)
        self.emp_id_entry.pack(pady=5)

        tk.Label(master, text="Select Schedule Interval (Mins):").pack(pady=5)
        self.interval_var = StringVar(master)
        self.interval_var.set(ALLOWED_INTERVALS[1]) 
        
        self.interval_menu = tk.OptionMenu(master, self.interval_var, *ALLOWED_INTERVALS)
        self.interval_menu.pack(pady=5)

        tk.Button(master, text="Start Tracking", command=self.login).pack(pady=10)

    def login(self):
        emp_id = self.emp_id_entry.get().strip()
        interval_str = self.interval_var.get()

        if not emp_id:
            messagebox.showerror("Validation Error", "Employee ID is mandatory.", parent=self.master)
            return

        try:
            interval = int(interval_str) 
        except ValueError:
            messagebox.showerror("Error", "Invalid interval selected.", parent=self.master)
            return

        USER_EMP_ID[0] = emp_id
        global POPUP_INTERVAL_MINUTES
        POPUP_INTERVAL_MINUTES = interval

        # Ensure login window is fully destroyed before starting main app loop
        self.master.destroy() 
        
        start_main_app(interval)


class ActivityApp:
    def __init__(self, master: tk.Tk, interval: int):
        self.master = master
        master.title(f"Activity Tracker (ID: {USER_EMP_ID[0]} | Interval: {interval}m)")
        self.interval = interval

        # Set initial and minimum geometry to prevent shrinking too small
        master.geometry("500x320") 
        master.minsize(500, 320) # Set minimum size
        master.protocol("WM_DELETE_WINDOW", self.hide_window) 

        # --- Status Display Label (Starts Blank, Reduced Size) ---
        self.status_display_label = tk.Label(master, 
                                            text="", 
                                            font=("Arial", 12, "bold"), # Reduced font size 
                                            pady=5)
        self.status_display_label.pack(pady=5)
        # -------------------------------

        self.task_label = tk.Label(master, text=DEFAULT_TASK_LABEL, font=("Arial", 12, "bold")) # Use constant
        self.task_label.pack(pady=5)

        self.task_entry = tk.Entry(master, width=60)
        self.task_entry.pack(pady=5, padx=10)
        
        tk.Label(master, text="(Enter task/reason, Mandatory for 'Working', 'Personal', 'Meeting')").pack()

        self.status_frame = tk.Frame(master)
        self.status_frame.pack(pady=10)

        self.create_status_buttons()
        self.master.after(100, self.initial_startup_log)
        self.master.after(10000, periodic_check, self)
        
        # Start the timer update loop
        self.master.after(1000, self.update_timer_display) 
        
        schedule_periodic_popup(self, self.interval)
        

    def create_status_buttons(self):
        button_info = [
            ("Working", "green"), ("Personal", "orange"), ("Break", "red"), 
            ("Lunch", "red"), ("Meeting", "orange"), ("Offline", "blue"), 
            ("Off work", "darkgreen")
        ]
        
        row_num = 0
        row_frame = None 
        
        for i, (status, color) in enumerate(button_info):
            if i % 3 == 0:
                row_num += 1
                row_frame = tk.Frame(self.status_frame)
                row_frame.pack(pady=5)
            
            if row_frame: 
                btn = tk.Button(row_frame, text=status, bg=color, fg="white", 
                                command=partial(self.submit_activity, status), 
                                width=10, font=("Arial", 10, "bold"))
                btn.pack(side=tk.LEFT, padx=5, pady=5)

    def update_status_display(self, status: str, timer_text: str = ""):
        """Updates the status label text, color, and appends the running timer."""
        status_lower = status.lower()
        
        # Determine base text and color
        if status_lower == "working":
            text = "On Work"
            color = "green"
        elif status_lower == "break":
            text = "On Break"
            color = "red"
        elif status_lower == "lunch":
            text = "On Lunch"
            color = "red"
        elif status_lower == "meeting":
            text = "In Meeting"
            color = "orange"
        elif status_lower == "personal":
            text = "On Personal Time"
            color = "orange"
        elif status_lower == "offline":
            text = "Offline"
            color = "blue"
        elif status_lower == "off work":
            text = "Off Work (Tracking 8h Limit)" 
            color = "darkgreen"
        elif status_lower == "idle":
            text = "Idle (No Activity)"
            color = "red"
        else:
            text = status
            color = "black"
        
        # Check for Exceeded status and override color/text if necessary
        if break_exceeded_flag and status in ["Break", "Lunch", "Personal", "Meeting", "Working", "Idle"]: 
             color = "red"
        
        # Concatenate the status text and the running timer
        display_text = f"Current Status: {text} {timer_text}"
        self.status_display_label.config(text=display_text, fg=color)
        
    def update_timer_display(self):
        """Calculates time remaining (Working/Timed) or time elapsed/exceeded (Timed/Off work) and handles blinking."""
        global TIMER_VISIBLE

        if self.master.winfo_ismapped() and self.master.wm_state() != 'iconic':
            
            status = current_status[0]
            timer_text = ""
            now = datetime.now()
            
            remaining_seconds = -1 # Sentinel value for remaining time
            
            if status == "Working" and not break_exceeded_flag:
                # --- WORKING: COUNTDOWN TO NEXT PROMPT (MM:SS) ---
                
                # Check if we have a remaining time from a previous break/lunch
                if WORK_REMAINING_SECONDS[0] > 0:
                    time_elapsed = now - last_response_time[0]
                    remaining_seconds = WORK_REMAINING_SECONDS[0] - int(time_elapsed.total_seconds())
                else:
                    # Fallback/Reset: Use full interval (normal schedule or after a full submission)
                    time_elapsed = now - last_response_time[0]
                    interval_seconds = POPUP_INTERVAL_MINUTES * 60
                    remaining_seconds = interval_seconds - int(time_elapsed.total_seconds())

                
                if remaining_seconds > 0:
                    minutes = remaining_seconds // 60
                    seconds = remaining_seconds % 60
                    
                    if remaining_seconds <= BLINK_THRESHOLD_SECONDS and not TIMER_VISIBLE:
                        timer_text = "" # Hide timer for blinking
                    else:
                        timer_text = f"({minutes:02d}:{seconds:02d} until prompt)"
                else:
                    timer_text = "(00:00 until prompt)"

            elif status in ["Break", "Lunch", "Meeting", "Personal"]:
                # --- TIMED STATUSES: COUNTDOWN or EXCEEDED TIME (HH:MM:SS) ---
                
                if TIMED_STATUS_END_TIME[0]:
                    time_remaining = TIMED_STATUS_END_TIME[0] - now
                    remaining_seconds = int(time_remaining.total_seconds())
                    
                    if remaining_seconds > 0:
                        # Display COUNTDOWN (MM:SS)
                        minutes = remaining_seconds // 60
                        seconds = remaining_seconds % 60
                        
                        if remaining_seconds <= BLINK_THRESHOLD_SECONDS and not TIMER_VISIBLE:
                             timer_text = "" # Hide timer for blinking
                        else:
                             timer_text = f"({minutes:02d}:{seconds:02d} remaining)"
                    else:
                        # Display EXCEEDED BY (HH:MM:SS)
                        time_exceeded = now - TIMED_STATUS_END_TIME[0]
                        exceeded_seconds = int(time_exceeded.total_seconds())
                        
                        hours = exceeded_seconds // 3600
                        minutes = (exceeded_seconds % 3600) // 60
                        seconds = exceeded_seconds % 60
                        timer_text = f"(Exceeded by: {hours:02d}:{minutes:02d}:{seconds:02d})"
                        
                else:
                    # Fallback for unexpected state - display elapsed time
                    time_elapsed = now - last_response_time[0]
                    total_seconds = int(time_elapsed.total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    timer_text = f"(Elapsed: {hours:02d}:{minutes:02d}:{seconds:02d})"


            elif status == "Off work":
                # --- OFF WORK: ELAPSED TIME (HH:MM:SS) against 8 hours ---
                if OFF_WORK_START_TIME[0]:
                    time_elapsed = now - OFF_WORK_START_TIME[0]
                    total_seconds = int(time_elapsed.total_seconds())
                    
                    limit_seconds = OFF_WORK_LIMIT_HOURS * 3600
                    
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    
                    if total_seconds < limit_seconds:
                         timer_text = f"({hours:02d}:{minutes:02d}:{seconds:02d} of {OFF_WORK_LIMIT_HOURS}h)"
                    else:
                         timer_text = f"(LIMIT EXCEEDED: {hours:02d}:{minutes:02d}:{seconds:02d})"
                else:
                    timer_text = "(Timer Pending Start)"

            elif status in ["Idle", "Offline"]:
                 timer_text = ""
            
            # Update the global visibility state for blinking
            if remaining_seconds >= 0 and remaining_seconds <= BLINK_THRESHOLD_SECONDS:
                TIMER_VISIBLE = not TIMER_VISIBLE
            else:
                 TIMER_VISIBLE = True
            
            # Update display with the current status and calculated timer
            self.update_status_display(status, timer_text)

        # Schedule the function to run again in 1000 milliseconds (1 second)
        self.master.after(1000, self.update_timer_display)


    def hide_window(self):
        """Minimizes the window to the taskbar (iconify) so it can be restored manually."""
        self.master.iconify() 
        self.master.attributes('-topmost', False)

    def show_window(self, message: str = DEFAULT_TASK_LABEL): # Use constant
        """Restores the window from the minimized state."""
        self.master.deiconify() 
        self.master.lift()
        self.master.focus_force()
        self.master.attributes('-topmost', True) 
        self.task_label.config(text=message)
        self.task_entry.delete(0, tk.END)
        # Update display when showing the window
        self.update_status_display(current_status[0]) 

    def submit_activity(self, status):
        global current_status, last_response_time, activity_thread, break_exceeded_flag, idle_check_flag, OFF_WORK_START_TIME, TIMED_STATUS_END_TIME, WORK_REMAINING_SECONDS

        response = self.task_entry.get().strip()
        log_remark = ""
        TIMED_STATUS_END_TIME[0] = None # Clear any previous end time

        # Flag to indicate if we should reset the clock (default is True for a new, productive submission)
        reset_work_clock = True 

        # --- Work Interval Pausing Logic (Saving remaining time when leaving "Working") ---
        if current_status[0] == "Working" and status != "Working":
            time_elapsed_seconds = (datetime.now() - last_response_time[0]).total_seconds()
            
            if WORK_REMAINING_SECONDS[0] > 0:
                remaining = WORK_REMAINING_SECONDS[0] - int(time_elapsed_seconds)
            else:
                full_interval_seconds = POPUP_INTERVAL_MINUTES * 60
                remaining = full_interval_seconds - int(time_elapsed_seconds)
            
            WORK_REMAINING_SECONDS[0] = max(0, remaining)
            log_remark = f"Work interval paused. {int(WORK_REMAINING_SECONDS[0]/60)}m remaining for next prompt."
            
            # Clock should not reset when user voluntarily pauses work
            reset_work_clock = False 

        # --- EXCEEDED/IDLE REASON VALIDATION (When returning to Working) ---
        if status == "Working" and break_exceeded_flag:
            
            is_returning_from_idle = current_status[0] == "Idle"
            if is_returning_from_idle:
                 exceeded_status_text = "Idle/Inactivity"
            else:
                 exceeded_status_text = current_status[0]

            reason = simpledialog.askstring(
                "Reason Required",
                f"You are exiting the EXCEEDED/IDLE status ({exceeded_status_text}). Please enter the reason for the exceedance/inactivity (mandatory):",
                parent=self.master
            )
            if not reason or not reason.strip():
                messagebox.showerror("Validation Error", "Reason for exceeding/inactivity is mandatory.", parent=self.master)
                # Re-display the window with the previous status and do not proceed
                self.show_window(f"Reason required to proceed from {current_status[0]} (EXCEEDED/IDLE)!")
                return
            
            # Log the exceedance resolution
            log_remark_exceed = f"Exceedance/Inactivity resolved: Back to {status}. Reason: {reason}"
            log_to_db(f"Exceedance/Inactivity resolved", current_status[0], log_remark_exceed)
            
            # --- IDLE/EXCEEDED RESET LOGIC (New logic to reset timer fully) ---
            
            # 1. Reset the remaining time to the full interval.
            WORK_REMAINING_SECONDS[0] = POPUP_INTERVAL_MINUTES * 60
            
            # 2. Ensure clock resets at the end of function.
            reset_work_clock = True 
            
            # --- END IDLE/EXCEEDED RESET LOGIC ---
            
            # Reset the task label to default before continuing submission
            self.task_label.config(text=DEFAULT_TASK_LABEL) 
        # ------------------------------------

        # --- Validation Rules for new status submission ---
        if status == "Working" and not response:
            messagebox.showwarning("Input Required", "Please enter a task or activity when status is 'Working'.", parent=self.master)
            return
        
        if status in ["Personal", "Meeting"] and not response:
            warning_msg = "Hey, you are not entering any reason for Personal / Meeting. This will intimate notification to your reporting manager & HR. Do you wish to proceed?"
            
            if not messagebox.askyesno("Reason Not Entered", warning_msg, parent=self.master):
                self.show_window("Please enter a reason or select a different status.")
                return 
            else:
                response = "(No Reason Entered - User Accepted Warning)"
                log_remark += f"| NO REASON ENTERED - Manager/HR Notification Intimated (Logging to DB/File)"
        
        # --- Status Submission ---
        
        if status == "Working":
            # If the submission is a regular "Working" status (not coming from exceed/idle):
            if not break_exceeded_flag and reset_work_clock:
                 # Ensure full interval starts if it's a fresh submission with no previous remaining time
                 if WORK_REMAINING_SECONDS[0] <= 0:
                      WORK_REMAINING_SECONDS[0] = POPUP_INTERVAL_MINUTES * 60 
            
        current_status[0] = status
        
        # Only log to DB if not already logged during the exceedance resolution step above
        if "Exceedance/Inactivity resolved" not in log_remark:
             log_to_db(response, status, remark=log_remark)
        
        # Reset the timestamp for the new activity (Start of the new interval/status)
        # ONLY reset if the clock should restart (regular log or return from IDLE/EXCEEDED)
        if reset_work_clock:
            last_response_time[0] = datetime.now()
        
        # --- Update the status label (called on successful submission) ---
        self.update_status_display(status) 

        # Clear the entry field after successful submission
        self.task_entry.delete(0, tk.END)

        # Reset flags upon *any* submission
        if break_exceeded_flag or idle_check_flag:
            break_exceeded_flag = False
            idle_check_flag = False
            
        # Clear Off Work Start Time if user goes back to work/break/etc
        if status != "Off work":
            OFF_WORK_START_TIME[0] = None

        if status == "Offline":
            log_to_db("Shutting Down", "Offline", "User manually logged off.")
            messagebox.showinfo("Offline Mode", "Offline Mode Activated. Program shutting down.")
            schedule.clear()
            sys.exit()

        elif status == "Off work":
            # Start monitoring 8-hour limit
            if OFF_WORK_START_TIME[0] is None:
                OFF_WORK_START_TIME[0] = datetime.now()
            
            # Ensure the scheduler is cleared so no popups happen
            schedule.clear()
            
            print("⏳ Off work Mode: Tracking 8-hour limit. No popups or idle checks.")
            self.hide_window() 
            return

        elif status in ["Break", "Lunch", "Meeting", "Personal"]:
            if status == "Break":
                duration = BREAK_DURATION_MINUTES
            elif status == "Lunch":
                duration = LUNCH_DURATION_MINUTES
            else: 
                duration_str = simpledialog.askstring(
                    f"{status} Duration",
                    f"Enter duration for {status} in minutes (e.g., 60):",
                    parent=self.master,
                )
                try:
                    duration = int(duration_str) if duration_str else 0
                except ValueError:
                    duration = 0
            
            if duration > 0:
                global activity_thread
                if activity_thread and activity_thread.is_alive():
                     print(f"⚠️ Cancelling previous timed status monitor.")
                
                # Set the global end time for the timer display before starting thread
                TIMED_STATUS_END_TIME[0] = datetime.now() + timedelta(minutes=duration)
                
                activity_thread = threading.Thread(target=monitor_timed_status, args=(datetime.now(), status, duration, self))
                log_to_db(response, status, f"Started for {duration} minutes.")
                print(f"⏳ {status} Mode: Prompts paused for {duration} minutes.")
                activity_thread.daemon = True
                activity_thread.start()
            else:
                log_to_db(response, status, "Duration not specified, defaulting to Working status.")
                current_status[0] = "Working"
                self.update_status_display("Working") # Update display on revert
                # Reset remaining time to full interval if reverting to working
                WORK_REMAINING_SECONDS[0] = POPUP_INTERVAL_MINUTES * 60 
                messagebox.showwarning("Status Change", f"Invalid or zero duration entered for {status}. Reverting to 'Working'.")

        self.hide_window()

    def initial_startup_log(self):
        """Logs the initial status (Working) and updates the display accordingly."""
        # Logs the initial log or checks for unexpected exit, setting global status to "Working"
        if not check_and_log_unexpected_exit():
            log_to_db("Startup", "Working", f"Initial program start for ID: {USER_EMP_ID[0]}.")
        
        # Initialize WORK_REMAINING_SECONDS to the full interval
        WORK_REMAINING_SECONDS[0] = POPUP_INTERVAL_MINUTES * 60
        
        # After the initial log runs, update the display to show the starting status.
        self.update_status_display(current_status[0])


# --- Scheduler Functions ---
def schedule_periodic_popup(app, interval):
    global POPUP_INTERVAL_MINUTES
    POPUP_INTERVAL_MINUTES = interval
    print(f"Periodic popup interval set to {interval} minutes.")


def check_and_show_popup(app):
    # This function is the scheduled target, which now checks the global timer state
    
    # Calculate current remaining time (replicated from update_timer_display for accuracy)
    if current_status[0] == "Working" and not break_exceeded_flag:
        time_elapsed_seconds = (datetime.now() - last_response_time[0]).total_seconds()
        
        if WORK_REMAINING_SECONDS[0] > 0:
            remaining_seconds = WORK_REMAINING_SECONDS[0] - int(time_elapsed_seconds)
        else:
            full_interval_seconds = POPUP_INTERVAL_MINUTES * 60
            remaining_seconds = full_interval_seconds - int(time_elapsed_seconds)
            
        if remaining_seconds <= 0:
            # Time for prompt has arrived
            
            # Reset the remaining time for the next full cycle
            WORK_REMAINING_SECONDS[0] = POPUP_INTERVAL_MINUTES * 60
            
            if not app.master.winfo_ismapped() or app.master.wm_state() == 'iconic':
                app.show_window(f"It's been {POPUP_INTERVAL_MINUTES} minutes. What's your current task?")
            else:
                print("Popup skipped: Window is already visible.")
                
            # Log the required action
            log_to_db("Prompt Displayed", "Working", f"Scheduled {POPUP_INTERVAL_MINUTES}m prompt shown.")
            
            # Set last_response_time to now to restart the countdown immediately
            last_response_time[0] = datetime.now()
        else:
            pass 
            
    else:
        pass

def run_schedule():
    # Fast check loop ensures prompt happens near exactly at 00:00
    while True:
        if current_status[0] == "Working" and not break_exceeded_flag:
            # Check if a prompt is due
            check_and_show_popup(app_instance) 
            
        time.sleep(1) 


# --- Timed Status Monitoring ---
def monitor_timed_status(start_time: datetime, status: str, duration_minutes: int, app):
    global current_status, break_exceeded_flag, break_check_start_time, LAST_EXCEED_LOG_TIME

    end_time = start_time + timedelta(minutes=duration_minutes)
    LAST_EXCEED_LOG_TIME[0] = None 
    
    while datetime.now() < end_time and current_status[0] == status:
        time.sleep(10)

    if current_status[0] == status:
        log_to_db(f"Exceeded {status} duration of {duration_minutes}m", status, "Time limit reached. Auto-transition to exceeded state.")
        break_exceeded_flag = True # Set flag for mandatory reason check
        break_check_start_time = datetime.now()
        
        while current_status[0] == status:
            now = datetime.now()
            
            # --- The timer display will calculate the exact exceeded time ---
            
            if LAST_EXCEED_LOG_TIME[0] is None or (now - LAST_EXCEED_LOG_TIME[0]).total_seconds() >= BREAK_EXCEED_LOG_INTERVAL:
                log_to_db(f"STILL Exceeded {status}", status, "Exceeded time limit. Logging this to DB every 10m.")
                LAST_EXCEED_LOG_TIME[0] = now
            
            if (now - break_check_start_time).total_seconds() >= BREAK_EXCEED_BUFFER_MINUTES * 60:
                if not app.master.winfo_ismapped() or app.master.wm_state() == 'iconic':
                    messagebox.showinfo("Status Check", f"Simple Reminder: You are still on {status} and exceeded the buffer time. Please update your status.")
                    app.show_window(f"Please update your status from exceeded {status} mode.")
                
            time.sleep(10)

        # Status changed by user, reset display back to current status (e.g., Working)
        app.update_status_display(current_status[0])


# --- Idle Monitoring ---
def monitor_idle(app):
    global idle_check_flag, last_response_time, break_exceeded_flag, current_status

    while True:
        time.sleep(5) 

        # Only check idle if currently "Working" and not already in an exceeded state
        if current_status[0] != "Working" or break_exceeded_flag or OFF_WORK_START_TIME[0] is not None:
            continue
            
        elapsed_since_response = datetime.now() - last_response_time[0]
        
        if elapsed_since_response.total_seconds() >= IDLE_TIMEOUT_SECONDS:
            
            mouse_before = pyautogui.position()
            time.sleep(IDLE_CAUTION_DELAY)
            mouse_after = pyautogui.position()

            if mouse_before == mouse_after:
                if not idle_check_flag:
                    # Update status display to show IDLE caution state
                    if app.master.winfo_ismapped() and app.master.wm_state() != 'iconic':
                        app.status_display_label.config(text="Current Status: IDLE (Caution)", fg="red")
                        
                    messagebox.showwarning("Caution!", "No mouse/keyboard activity detected for 10 minutes. Please update your status or move your mouse.", parent=app.master)
                    idle_check_flag = True 
                
                if (datetime.now() - last_response_time[0]).total_seconds() >= IDLE_TIMEOUT_SECONDS + IDLE_CAUTION_DELAY:
                    # Extended Idle detected, force status change and set exceed flag
                    log_to_db("Extended Idle Logged", "Idle", f"No mouse/keyboard activity. Status changed to IDLE. Mandatory reason on return to work.")
                    
                    # Set flags for mandatory reason on return
                    current_status[0] = "Idle" 
                    break_exceeded_flag = True 
                    
                    # Update status display to show definite IDLE state
                    if app.master.winfo_ismapped() and app.master.wm_state() != 'iconic':
                         app.update_status_display("Idle")
                    
                    # Reset last_response_time so IDLE monitor tracks the duration of IDLE state.
                    last_response_time[0] = datetime.now()
                    idle_check_flag = False 

            else:
                if idle_check_flag:
                    log_to_db("Movement Detected", "Working", "Activity detected after extended idle caution.")
                    # Update status display back to Working
                    app.update_status_display("Working")
                    
                    idle_check_flag = False 
                    last_response_time[0] = datetime.now()


# --- Periodic Check Scheduler (Main loop glue) ---
def periodic_check(app):
    app.master.after(10000, periodic_check, app)


# --- Main Entry Point ---
def start_main_app(interval: int):
    global root, app_instance
    root = tk.Tk()
    app_instance = ActivityApp(root, interval)

    scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
    scheduler_thread.start()
    
    idle_thread = threading.Thread(target=monitor_idle, args=(app_instance,), daemon=True)
    idle_thread.start()

    root.mainloop()


def main():
    """Handles the initial cleanup and launches the login screen."""
    
    # Clean existing DB file on each run for a fresh start
    if os.path.exists(LOG_DB_FILE):
        try:
            os.remove(LOG_DB_FILE)
            print(f"🧹 Cleaned old database file: {LOG_DB_FILE}")
        except PermissionError:
            print(f"⚠️ Warning: Cannot clean DB. File is likely open/locked by another process.")

    # 1. Initialize the SQLite database (creates a new file)
    init_db()
        
    # 2. Launch login screen first
    login_root = tk.Tk()
    LoginApp(login_root)
    # The login process now calls login_root.destroy() before main app starts.
    login_root.mainloop()


if __name__ == "__main__":
    main()