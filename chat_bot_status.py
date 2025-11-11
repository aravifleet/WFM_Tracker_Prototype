import os
import pyautogui
import sqlite3
from datetime import datetime, timedelta
import csv
import time
import schedule
import threading
import sys 

# --- Configuration ---
os.environ['TK_SILENCE_DEPRECATION'] = '1'
DB_FILE = "responses.db"
CSV_FILE = "Responses_log.csv"
POPUP_INTERVAL_MINUTES = 0 
IDLE_TIMEOUT_SECONDS = 300 # 5 minutes (300 seconds) for standard idle
BREAK_RESPONSE_TIMEOUT_SECONDS = 300 # 5 minutes timeout for break response grace period
BREAK_DURATION_MINUTES = 15
LUNCH_DURATION_MINUTES = 30
STATUS_OPTIONS = ["Working", "Lunch", "Meeting", "Personal", "Break", "Offline"]


# --- Global State ---
last_response_time = [datetime.now()]
current_status = ["Working"]
user_status_updated = threading.Event()
activity_thread = None
break_exceeded_flag = False 
idle_check_flag = False 
break_check_start_time = None 
# --- Global State Ends ---


# --- Utility Functions ---
def setup_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, response TEXT, status TEXT, remark TEXT
            )''')
    c.execute('DELETE FROM responses')
    conn.commit()
    conn.close()
    print("✨ Database cleaned successfully for this run.")
    try:
        with open(CSV_FILE, mode="w", newline="") as file: 
            csv.writer(file).writerow(["Timestamp", "Response", "Status", "Remark"])
        print("✨ CSV log file reset successfully for this run.")
    except Exception as e:
        print(f"⚠️ Warning: Could not reset CSV file ({e}).")

def log_data(response, status, remark=""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO responses (timestamp, response, status, remark) VALUES (?, ?, ?, ?)",
              (timestamp, response, status, remark))
    conn.commit()
    conn.close()
    with open(CSV_FILE, mode="a", newline="") as file:
        csv.writer(file).writerow([timestamp, response, status, remark])
    print(f"[{timestamp}] ✅ Logged: {response} | Status: **{status}** | Remark: {remark}")

def ask_user_response(message="Hey there 👋\nWhat are you working on right now?"):
    try:
        response = pyautogui.prompt(message, title="Activity Tracker") # type: ignore
        return response.strip() if response else None
    except Exception as e:
        print(f"Error showing prompt: {e}")
        return None

def ask_user_status(text="Select your current status:"):
    try:
        status = pyautogui.confirm(text=text, title="Status Selection", buttons=STATUS_OPTIONS) # type: ignore
        return status if status else "Working"
    except Exception as e:
        print(f"Error showing status confirm: {e}")
        return "Working"

def ask_break_over_status(status):
    """Asks user if they are back to work after their break time has expired."""
    prompt_text = f"🛑 Your **{status}** time is over.\n\nAre you back to work, buddy?"
    try:
        return pyautogui.confirm(text=prompt_text, title="Break Over Check", buttons=["Yes, Back to Work", "Still on Break (5 min check)"]) # type: ignore
    except Exception:
        return None 

def ask_on_track():
    """Asks if the user is present after 5+ minutes of inactivity."""
    try:
        return pyautogui.confirm(text="⚠️ No mouse/keyboard movement for 5+ minutes.\n\nAre you there???", # type: ignore
            title="Activity Check", buttons=["Yes, Back on Track", "No, Still Idle"]) # type: ignore
    except Exception:
        return None 
    
# --- Status Monitoring (Timed Status) ---
def monitor_timed_status(start_time, status, duration_minutes):
    global user_status_updated, current_status, break_exceeded_flag, break_check_start_time
    
    end_time = start_time + timedelta(minutes=duration_minutes)
    while datetime.now() < end_time:
        if user_status_updated.is_set(): return
        time.sleep(10)

    log_data(f"Exceeded {status}", status, f"Time limit reached. Auto-transitioning to Exceeded status check.")
    
    current_status[0] = status # Keep status as break status temporarily
    break_exceeded_flag = True
    break_check_start_time = datetime.now() # START THE GRACE PERIOD TIMER
    user_status_updated.set()
    return


# --- Idle Monitoring ---
def monitor_idle(last_response_time, current_status):
    global IDLE_TIMEOUT_SECONDS, idle_check_flag
    
    while True:
        time.sleep(10)
        if current_status[0] != "Working" or break_exceeded_flag:
            continue

        elapsed = datetime.now() - last_response_time[0]
        
        if elapsed.total_seconds() >= IDLE_TIMEOUT_SECONDS:
            mouse_position_before_idle = pyautogui.position()
            time.sleep(10) 
            mouse_position_after_idle = pyautogui.position()
            
            if mouse_position_before_idle == mouse_position_after_idle:
                log_data("Idle", "Idle", f"No response/movement for {IDLE_TIMEOUT_SECONDS}s")
                last_response_time[0] = datetime.now() 
            else:
                log_data("Movement Detected", "Working", "Activity detected after extended idle. Preparing safe check.")
                idle_check_flag = True 
                last_response_time[0] = datetime.now() 


# --- Flag Handler (CRITICAL REVISIONS for Exceeded Time Calculation) ---
def handle_flags():
    global current_status, break_exceeded_flag, idle_check_flag, last_response_time, break_check_start_time

    # --- 1. Break Exceeded & Grace Period Logic ---
    if break_exceeded_flag:
        # Ensure break_check_start_time is a datetime object
        if break_check_start_time is None:
             break_exceeded_flag = False
             return True

        elapsed_break_check = datetime.now() - break_check_start_time
        current_break_status = current_status[0]

        # A. 5-Minute Non-Response Timeout 
        if elapsed_break_check.total_seconds() >= BREAK_RESPONSE_TIMEOUT_SECONDS:
            log_data("No response", "Idle", f"No response to break over check for {BREAK_RESPONSE_TIMEOUT_SECONDS}s. Logging as IDLE.")
            break_exceeded_flag = False 
            current_status[0] = "Working" 
            break_check_start_time = None
            last_response_time[0] = datetime.now()
            return True 
        
        # B. Prompt User (Re-ask on movement or first time)
        mouse_moved = True 
        if elapsed_break_check.total_seconds() > 15: 
             mouse_pos_now = pyautogui.position()
             time.sleep(1) 
             mouse_pos_later = pyautogui.position()
             mouse_moved = (mouse_pos_now != mouse_pos_later)

        if mouse_moved or elapsed_break_check.total_seconds() < 15:
            user_response = ask_break_over_status(current_break_status) 
            
            # --- CALCULATE EXCEEDED TIME BEFORE RESETTING THE TIMER ---
            time_responded = datetime.now() 
            time_exceeded_delta = time_responded - break_check_start_time
            
            # Format the exceeded time (Hours:Minutes:Seconds)
            total_seconds = int(time_exceeded_delta.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            exceeded_remark = f"Exceeded time: {hours}h {minutes}m {seconds}s." if total_seconds > 0 else "Returned immediately."
            # ---------------------------------------------------------
            
            break_check_start_time = datetime.now() # Reset grace period timer after asking

            if user_response == "Yes, Back to Work": 
                task_response = ask_user_response("You selected to resume work. What task are you doing now?")
                # MODIFIED: Use the calculated remark
                log_data(task_response if task_response else "No task provided", "Working", f"Status resumed. {exceeded_remark}") 
                
                break_exceeded_flag = False 
                current_status[0] = "Working"
                break_check_start_time = None
                last_response_time[0] = datetime.now()
                return True

            elif user_response == "Still on Break (5 min check)":
                log_data("Break Check Confirmed", current_break_status, f"Break continuation confirmed. 5 minute grace period started. {exceeded_remark}")
                return True 
                
            elif user_response is None: 
                log_data("Prompt Ignored/Cancelled", current_break_status, f"Grace period started/continued. {exceeded_remark}")
                return True 
        
        return True 

    # --- 2. Idle Check Logic ---
    if idle_check_flag:
        
        if current_status[0] != "Working":
            idle_check_flag = False
            return True 
            
        user_response = ask_on_track() 
        
        if user_response == "Yes, Back on Track":
            log_data("Resumed Work", "Working", "User confirmed being back on track after idle.")
        else: 
             log_data("Did Not Confirm", "Working/Idle-Check", "User did not confirm being back on track.")

        idle_check_flag = False
        current_status[0] = "Working"
        last_response_time[0] = datetime.now() 
        return True 
        
    return False 

# --- Main Chatbot Function (Priority Handling) ---
# --- Main Chatbot Function (Priority Handling) ---
def chatbot_run():
    global user_status_updated, activity_thread, current_status, idle_check_flag, break_exceeded_flag
    
    # 1. Check if a flag needs handling (Break Exceed or Idle Check)
    if break_exceeded_flag or idle_check_flag:
        if handle_flags():
            return 
        
    # 2. Regular Scheduled Chatbot Run Logic 
    
    if user_status_updated.is_set():
        user_status_updated = threading.Event()
        
    response = ask_user_response()
    
    if response is None:
        # **MODIFIED LOGIC:** Log the ignored prompt, but keep the status as Working.
        # This prevents an immediate false "Idle" log, allowing the physical monitor (monitor_idle)
        # to confirm true idleness via mouse/keyboard detection later.
        log_data("Prompt Ignored", "Working", f"No response to scheduled prompt ({POPUP_INTERVAL_MINUTES}m interval).")
        last_response_time[0] = datetime.now()
        current_status[0] = "Working" 
        
    else:
        status = ask_user_status(f"You entered: '{response}'. Select your current status:")
        current_status[0] = status
        
        log_data(response, status)
        last_response_time[0] = datetime.now()
        
        if status in ["Break", "Lunch", "Meeting", "Personal", "Offline"]:
            idle_check_flag = False 
        
        if status == "Offline":
            print("\n🛑 **Offline Mode Activated:** No further prompts will appear until the program is restarted.")
            schedule.clear()
            sys.exit() 
            
        elif status in ["Break", "Lunch", "Meeting", "Personal"]:
            duration = BREAK_DURATION_MINUTES if status == "Break" else LUNCH_DURATION_MINUTES
            if status in ["Meeting", "Personal"]:
                 duration_str = pyautogui.prompt(f"Enter duration for **{status}** in minutes (e.g., 60):", title=f"{status} Duration") # type: ignore
                 try:
                     duration = int(duration_str) if duration_str else 0
                 except ValueError:
                     duration = 0
            
            if duration > 0:
                activity_thread = threading.Thread(target=monitor_timed_status, args=(datetime.now(), status, duration))
                log_data(response, status, f"Started for {duration} minutes.")
                print(f"⏳ **{status} Mode:** Prompts paused for {duration} minutes.")
                activity_thread.daemon = True
                activity_thread.start()
            else:
                 log_data(response, status, "Duration not specified, defaulting to Working status log.")
                 current_status[0] = "Working"


# --- Scheduler Logic ---
def scheduler_logic(interval_minutes):
    global POPUP_INTERVAL_MINUTES
    POPUP_INTERVAL_MINUTES = interval_minutes
    print(f"✅ Chatbot scheduler started — runs every **{interval_minutes}** minute(s).")
    schedule.every(interval_minutes).minutes.do(chatbot_run)
    
    while True:
        try:
            if current_status[0] == "Working":
                schedule.run_pending() 
            
            if break_exceeded_flag or idle_check_flag:
                handle_flags()

            time.sleep(1) 
            
        except KeyboardInterrupt:
            print("\n🛑 Scheduler stopped by user.")
            break
        except Exception as e:
            print(f"An unexpected error occurred in the scheduler: {e}")
            break


# --- Entry Point ---
if __name__ == "__main__":
    setup_database()
    
    try:
        interval = float(input("Enter pop-up interval in minutes (e.g., 1, 5, 10, up to 60 or more): "))
        if interval <= 0:
            print("❌ Interval must be greater than zero. Defaulting to 30 minutes.")
            interval = 30.0
            
        idle_thread = threading.Thread(target=monitor_idle, args=(last_response_time, current_status))
        idle_thread.daemon = True
        idle_thread.start()

        chatbot_run()
        scheduler_logic(interval)
        
    except ValueError:
        print("❌ Invalid input. Please enter a numeric value for interval.")
    except Exception as e:
        print(f"An error occurred during startup: {e}")