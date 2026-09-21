import asyncio
import csv
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import requests
import tkintermapview
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_FILE = "fleet_tracker.db"
DEFAULT_LAT = 17.7289
DEFAULT_LNG = 83.3034
GPS_UPDATE_INTERVAL = 3
REMOTE_POLL_INTERVAL = 3
STALE_LOCATION_SECONDS = 120

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()

def supabase_enabled():
    return bool(
        SUPABASE_URL.startswith("https://")
        and ".supabase.co" in SUPABASE_URL
        and SUPABASE_ANON_KEY
        and "YOUR-" not in SUPABASE_ANON_KEY
    )

def supabase_headers():
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }

def parse_utc_timestamp(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def clean_remote_bus_rows(rows):
    cleaned = []
    if not isinstance(rows, list):
        return cleaned
    for row in rows:
        if not isinstance(row, dict):
            continue
        driver = str(row.get("driver", "")).strip()
        if not driver or driver.lower() in {"none", "null", "undefined"}:
            continue
        try:
            lat, lng = float(row["lat"]), float(row["lng"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        item = dict(row)
        item.update(driver=driver, lat=lat, lng=lng)
        cleaned.append(item)
    return cleaned

def find_latest_live_driver(buses):
    now = datetime.now(timezone.utc)
    for bus in clean_remote_bus_rows(buses):
        stamp = parse_utc_timestamp(bus.get("updated_at"))
        if stamp and (now - stamp).total_seconds() <= STALE_LOCATION_SECONDS:
            return bus["driver"], bus["lat"], bus["lng"]
    return None

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS location_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            timestamp DATETIME NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS config(
            key TEXT PRIMARY KEY, value TEXT)""")
        conn.execute(
            "INSERT OR IGNORE INTO config(key,value) VALUES('global_gps','ON')"
        )

def upload_driver_location(driver, lat, lng):
    if not supabase_enabled():
        return False
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/bus_locations"
    payload = {
        "driver": str(driver),
        "lat": float(lat),
        "lng": float(lng),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = requests.patch(
            url, headers=supabase_headers(),
            params={"driver": f"eq.{driver}"},
            json=payload, timeout=10)
        if response.status_code not in (200, 204):
            return False
        if response.text.strip() in ("", "[]"):
            response = requests.post(
                url, headers=supabase_headers(),
                json=payload, timeout=10)
        return response.status_code in (200, 201, 204)
    except requests.RequestException as exc:
        print("Location upload error:", exc)
        return False

def get_remote_bus_locations():
    if not supabase_enabled():
        return []
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/bus_locations"
    try:
        response = requests.get(
            url, headers=supabase_headers(),
            params={"select": "driver,lat,lng,updated_at", "order": "updated_at.desc"},
            timeout=10)
        if response.status_code != 200:
            print("Supabase download error:", response.status_code, response.text)
            return []
        return clean_remote_bus_rows(response.json())
    except (requests.RequestException, ValueError) as exc:
        print("Remote location error:", exc)
        return []

def delete_remote_driver(driver):
    if not supabase_enabled() or not driver:
        return
    try:
        requests.delete(
            SUPABASE_URL.rstrip("/") + "/rest/v1/bus_locations",
            headers=supabase_headers(),
            params={"driver": f"eq.{driver}"},
            timeout=10)
    except requests.RequestException as exc:
        print("Remote delete error:", exc)

def save_location(driver, lat, lng):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO location_logs(driver,lat,lng,timestamp) VALUES(?,?,?,?)",
                (driver, float(lat), float(lng), datetime.now(timezone.utc).isoformat()))
    except sqlite3.Error as exc:
        print("Local save error:", exc)

class FleetTrackerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BUS MAPS")
        self.geometry("1200x750")
        self.minsize(1000, 650)
        self.configure(bg="#121212")
        self.current_user = None
        self.current_role = None
        self.container = tk.Frame(self, bg="#121212")
        self.container.pack(fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)
        self.frames = {}
        for cls in (LoginScreen, DeveloperScreen, DriverScreen, StudentScreen):
            frame = cls(self.container, self)
            self.frames[cls.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")
        self.show_frame("LoginScreen")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def show_frame(self, name):
        frame = self.frames[name]
        frame.tkraise()
        if hasattr(frame, "on_show"):
            frame.on_show()

    def logout(self):
        self.current_user = None
        self.current_role = None
        self.show_frame("LoginScreen")

class LoginScreen(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#121212")
        self.controller = controller
        box = tk.Frame(self, bg="#1e1e1e", padx=45, pady=45)
        box.place(relx=.5, rely=.5, anchor="center")
        tk.Label(box, text="🚌 BUS MAPS", font=("Arial",26,"bold"),
                 bg="#1e1e1e", fg="white").pack(pady=(0,8))
        tk.Label(box, text="Live Fleet Tracking", bg="#1e1e1e",
                 fg="#888888").pack(pady=(0,25))
        tk.Label(box, text="Username", bg="#1e1e1e", fg="#bbbbbb").pack(anchor="w")
        self.user_entry = tk.Entry(box, font=("Arial",14), width=28)
        self.user_entry.pack(pady=(5,15))
        tk.Label(box, text="Password", bg="#1e1e1e", fg="#bbbbbb").pack(anchor="w")
        self.pass_entry = tk.Entry(box, font=("Arial",14), width=28, show="*")
        self.pass_entry.pack(pady=(5,25))
        tk.Button(box, text="SECURE LOGIN", bg="#007bff", fg="white",
                  font=("Arial",12,"bold"), command=self.authenticate,
                  width=24, pady=8, relief="flat").pack()
        self.pass_entry.bind("<Return>", lambda e: self.authenticate())

    def on_show(self):
        self.user_entry.delete(0, tk.END)
        self.pass_entry.delete(0, tk.END)
        self.user_entry.focus_set()

    def authenticate(self):
        username = self.user_entry.get().strip()
        password = self.pass_entry.get()
        if not username or not password:
            messagebox.showwarning("Login", "Enter username and password.")
            return
        admin_user = os.getenv("BUSMAPS_ADMIN_USER", "")
        admin_pass = os.getenv("BUSMAPS_ADMIN_PASSWORD", "")
        if admin_user and username == admin_user and password == admin_pass:
            self.controller.current_user = username
            self.controller.current_role = "developer"
            self.controller.show_frame("DeveloperScreen")
            return
        try:
            with get_db_connection() as conn:
                user = conn.execute(
                    "SELECT * FROM users WHERE username=?", (username,)).fetchone()
        except sqlite3.Error as exc:
            messagebox.showerror("Database Error", str(exc))
            return
        if user:
            try:
                valid = check_password_hash(user["password"], password)
            except Exception:
                valid = False
            if valid and user["role"] in {"driver","student"}:
                self.controller.current_user = username
                self.controller.current_role = user["role"]
                self.controller.show_frame(
                    "DriverScreen" if user["role"] == "driver" else "StudentScreen")
                return
        messagebox.showerror("Login Failed", "Invalid username or password.")

class MapMixin:
    def setup_map(self, parent):
        self.map_widget = tkintermapview.TkinterMapView(parent, corner_radius=0)
        self.map_widget.pack(fill="both", expand=True)
        self.map_widget.set_tile_server(
            "https://mt0.google.com/vt/lyrs=m&hl=en&x={x}&y={y}&z={z}&s=Ga",
            max_zoom=22)
        self.map_widget.set_position(DEFAULT_LAT, DEFAULT_LNG)
        self.map_widget.set_zoom(12)

    def center_driver(self):
        live = find_latest_live_driver(self.latest_live_buses)
        if live:
            driver, lat, lng = live
            self.map_widget.set_position(lat, lng)
            self.map_widget.set_zoom(17)
        else:
            threading.Thread(target=self._fetch_and_center, daemon=True).start()

    def _fetch_and_center(self):
        live = find_latest_live_driver(get_remote_bus_locations())
        self.after(0, lambda: self._finish_center(live))

    def _finish_center(self, live):
        if not live:
            messagebox.showinfo("Driver Location",
                                "No live driver location is available right now.")
            return
        _, lat, lng = live
        self.map_widget.set_position(lat, lng)
        self.map_widget.set_zoom(17)

class DeveloperScreen(tk.Frame, MapMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#121212")
        self.controller = controller
        self.latest_live_buses = []
        self.markers = {}
        header = tk.Frame(self, bg="#1e1e1e", pady=12, padx=20)
        header.pack(fill="x")
        tk.Label(header, text="Developer Workspace", font=("Arial",17,"bold"),
                 bg="#1e1e1e", fg="white").pack(side="left")
        tk.Button(header, text="Logout", bg="#dc3545", fg="white",
                  command=controller.logout).pack(side="right")
        main = tk.Frame(self, bg="#121212")
        main.pack(fill="both", expand=True, padx=15, pady=15)
        left = tk.Frame(main, bg="#1e1e1e", width=300)
        left.pack(side="left", fill="y", padx=(0,15))
        left.pack_propagate(False)
        tk.Label(left,text="Provision Users",font=("Arial",14,"bold"),
                 bg="#1e1e1e",fg="white").pack(anchor="w",padx=15,pady=15)
        self.new_user = tk.Entry(left); self.new_user.insert(0,"Username")
        self.new_user.pack(fill="x",padx=15,pady=5)
        self.new_pass = tk.Entry(left,show="*")
        self.new_pass.pack(fill="x",padx=15,pady=5)
        self.role = tk.StringVar(value="driver")
        ttk.Combobox(left,textvariable=self.role,values=("driver","student"),
                     state="readonly").pack(fill="x",padx=15,pady=5)
        tk.Button(left,text="Create Account",bg="#28a745",fg="white",
                  command=self.create_user).pack(fill="x",padx=15,pady=8)
        self.users = tk.Listbox(left,bg="#2a2a2a",fg="white",height=8)
        self.users.pack(fill="x",padx=15,pady=5)
        tk.Button(left,text="Delete Selected User",bg="#dc3545",fg="white",
                  command=self.delete_user).pack(fill="x",padx=15)
        tk.Button(left,text="📍 Driver Live Location",bg="#007bff",fg="white",
                  command=self.center_driver).pack(fill="x",padx=15,pady=15)
        right = tk.Frame(main,bg="#121212")
        right.pack(side="right",fill="both",expand=True)
        self.setup_map(right)
        tk.Button(right,text="Export Location Logs",bg="#17a2b8",fg="white",
                  command=self.export_csv).pack(anchor="e")

    def on_show(self):
        self.refresh_users()
        self.poll_map()

    def refresh_users(self):
        self.users.delete(0,tk.END)
        with get_db_connection() as conn:
            rows=conn.execute("SELECT id,username,role FROM users ORDER BY role,username").fetchall()
        for row in rows:
            self.users.insert(tk.END,f"[{row['role'].upper()}] {row['username']} (ID:{row['id']})")

    def create_user(self):
        username=self.new_user.get().strip()
        password=self.new_pass.get()
        if not username or not password:
            messagebox.showwarning("Input Error","Enter username and password.")
            return
        try:
            with get_db_connection() as conn:
                conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                             (username,generate_password_hash(password),self.role.get()))
            self.refresh_users()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error","Username already exists.")

    def delete_user(self):
        sel=self.users.curselection()
        if not sel: return
        try: user_id=int(self.users.get(sel[0]).split("ID:")[1].split(")")[0])
        except (ValueError,IndexError): return
        if messagebox.askyesno("Delete","Delete this user?"):
            with get_db_connection() as conn:
                conn.execute("DELETE FROM users WHERE id=?",(user_id,))
            self.refresh_users()

    def export_csv(self):
        path=filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV Files","*.csv")])
        if not path: return
        with get_db_connection() as conn:
            rows=conn.execute("SELECT id,driver,lat,lng,timestamp FROM location_logs ORDER BY timestamp DESC").fetchall()
        with open(path,"w",newline="",encoding="utf-8") as f:
            writer=csv.writer(f); writer.writerow(("ID","Driver","Latitude","Longitude","Timestamp"))
            writer.writerows([tuple(row) for row in rows])
        messagebox.showinfo("Success","Export complete.")

    def poll_map(self):
        if self.controller.current_role!="developer": return
        threading.Thread(target=self._download,daemon=True).start()

    def _download(self):
        buses=get_remote_bus_locations()
        self.after(0,self.update_markers,buses)

    def update_markers(self,buses):
        self.latest_live_buses=clean_remote_bus_rows(buses)
        active=set()
        now=datetime.now(timezone.utc)
        for bus in self.latest_live_buses:
            stamp=parse_utc_timestamp(bus.get("updated_at"))
            if not stamp or (now-stamp).total_seconds()>STALE_LOCATION_SECONDS: continue
            driver=bus["driver"]; active.add(driver)
            if driver in self.markers: self.markers[driver].set_position(bus["lat"],bus["lng"])
            else: self.markers[driver]=self.map_widget.set_marker(bus["lat"],bus["lng"],text=f"🚌 LIVE BUS: {driver}")
        for driver in list(self.markers):
            if driver not in active:
                try: self.markers[driver].delete()
                except Exception: pass
                del self.markers[driver]
        self.after(REMOTE_POLL_INTERVAL*1000,self.poll_map)

class DriverScreen(tk.Frame):
    def __init__(self,parent,controller):
        super().__init__(parent,bg="#121212")
        self.controller=controller; self.tracking=False
        self.lat=DEFAULT_LAT; self.lng=DEFAULT_LNG; self.marker=None
        header=tk.Frame(self,bg="#1e1e1e",pady=12,padx=20); header.pack(fill="x")
        self.title_lbl=tk.Label(header,text="Driver Console",font=("Arial",17,"bold"),bg="#1e1e1e",fg="white")
        self.title_lbl.pack(side="left")
        tk.Button(header,text="Logout",bg="#dc3545",fg="white",command=controller.logout).pack(side="right")
        controls=tk.Frame(self,bg="#2a2a2a",pady=12); controls.pack(fill="x")
        self.status=tk.Label(controls,text="🔴 Not Transmitting",font=("Arial",14,"bold"),bg="#2a2a2a",fg="#dc3545")
        self.status.pack()
        self.coords=tk.Label(controls,text="Location: --",bg="#2a2a2a",fg="#aaa"); self.coords.pack()
        buttons=tk.Frame(controls,bg="#2a2a2a"); buttons.pack(pady=6)
        tk.Button(buttons,text="📡 Start Live Tracking",bg="#28a745",fg="white",command=self.start_tracking).pack(side="left",padx=5)
        tk.Button(buttons,text="⛔ Stop",bg="#dc3545",fg="white",command=self.stop_tracking).pack(side="left",padx=5)
        self.map_widget=tkintermapview.TkinterMapView(self,corner_radius=0)
        self.map_widget.pack(fill="both",expand=True)
        self.map_widget.set_tile_server("https://mt0.google.com/vt/lyrs=m&hl=en&x={x}&y={y}&z={z}&s=Ga",max_zoom=22)

    def on_show(self):
        self.title_lbl.config(text=f"Driver: {self.controller.current_user}")
        self.map_widget.set_position(self.lat,self.lng); self.map_widget.set_zoom(14)
        if self.marker:
            try: self.marker.delete()
            except Exception: pass
        self.marker=self.map_widget.set_marker(self.lat,self.lng,text="Driver")

    def start_tracking(self):
        self.stop_tracking(silent=True)
        if not supabase_enabled():
            messagebox.showwarning("Supabase","Configure SUPABASE_URL and SUPABASE_ANON_KEY first.")
            return
        self.tracking=True
        self.status.config(text="⏳ Acquiring Live Location...",fg="#ffc107")
        threading.Thread(target=self.location_loop,daemon=True).start()

    def location_loop(self):
        try:
            import winrt.windows.devices.geolocation as wdg
        except ImportError:
            self.after(0,self.location_failed,"Install the winrt package and enable Windows Location Services.")
            return
        while self.tracking:
            try:
                async def get_location():
                    locator=wdg.Geolocator()
                    locator.desired_accuracy=wdg.PositionAccuracy.HIGH
                    position=await locator.get_geoposition_async()
                    return position.coordinate.latitude,position.coordinate.longitude
                lat,lng=asyncio.run(get_location())
                self.after(0,self.apply_location,lat,lng)
            except Exception as exc:
                self.after(0,self.location_failed,str(exc))
                return
            for _ in range(GPS_UPDATE_INTERVAL*10):
                if not self.tracking: break
                time.sleep(.1)

    def apply_location(self,lat,lng):
        if not self.tracking:return
        self.lat,self.lng=float(lat),float(lng)
        self.status.config(text="🟢 LIVE TRACKING • ONLINE",fg="#28a745")
        self.coords.config(text=f"{self.lat:.6f}, {self.lng:.6f}")
        self.map_widget.set_position(self.lat,self.lng); self.map_widget.set_zoom(16)
        if self.marker:self.marker.set_position(self.lat,self.lng); self.marker.set_text("🚌 REAL LOCATION")
        else:self.marker=self.map_widget.set_marker(self.lat,self.lng,text="🚌 REAL LOCATION")
        save_location(self.controller.current_user,self.lat,self.lng)
        threading.Thread(target=upload_driver_location,args=(self.controller.current_user,self.lat,self.lng),daemon=True).start()

    def stop_tracking(self,silent=False):
        was=self.tracking; self.tracking=False
        if was:
            threading.Thread(target=delete_remote_driver,args=(self.controller.current_user,),daemon=True).start()
        if not silent:
            self.status.config(text="🔴 Not Transmitting",fg="#dc3545"); self.coords.config(text="Location: --")

    def location_failed(self,error):
        self.tracking=False
        self.status.config(text="🔴 GPS FAILED",fg="#dc3545")
        messagebox.showerror("GPS Error",f"Could not obtain your location.\n\nCheck Windows Location Services.\n\n{error}")

class StudentScreen(tk.Frame,MapMixin):
    def __init__(self,parent,controller):
        super().__init__(parent,bg="#121212")
        self.controller=controller; self.latest_live_buses=[]; self.markers={}
        header=tk.Frame(self,bg="#1e1e1e",pady=12,padx=20); header.pack(fill="x")
        self.title_lbl=tk.Label(header,text="Student — LIVE BUSES",font=("Arial",17,"bold"),bg="#1e1e1e",fg="white")
        self.title_lbl.pack(side="left")
        tk.Label(header,text="● ONLINE",font=("Arial",9,"bold"),bg="#1e1e1e",fg="#28a745").pack(side="left",padx=15)
        tk.Button(header,text="Logout",bg="#dc3545",fg="white",command=controller.logout).pack(side="right")
        self.setup_map(self)
        tk.Button(self,text="📍 Driver Live Location",bg="#007bff",fg="white",
                  font=("Arial",10,"bold"),command=self.center_driver).place(x=15,y=70)

    def on_show(self):
        self.title_lbl.config(text=f"Student — {self.controller.current_user}")
        self.poll_remote_buses()

    def poll_remote_buses(self):
        if self.controller.current_role!="student": return
        threading.Thread(target=self._download,daemon=True).start()

    def _download(self):
        self.after(0,self.update_markers,get_remote_bus_locations())

    def update_markers(self,buses):
        self.latest_live_buses=clean_remote_bus_rows(buses)
        active=set(); now=datetime.now(timezone.utc)
        for bus in self.latest_live_buses:
            stamp=parse_utc_timestamp(bus.get("updated_at"))
            if not stamp or (now-stamp).total_seconds()>STALE_LOCATION_SECONDS: continue
            driver=bus["driver"]; active.add(driver)
            if driver in self.markers:self.markers[driver].set_position(bus["lat"],bus["lng"])
            else:self.markers[driver]=self.map_widget.set_marker(bus["lat"],bus["lng"],text=f"🚌 BUS: {driver}")
        for driver in list(self.markers):
            if driver not in active:
                try:self.markers[driver].delete()
                except Exception:pass
                del self.markers[driver]
        self.after(REMOTE_POLL_INTERVAL*1000,self.poll_remote_buses)

if __name__=="__main__":
    init_db()
    FleetTrackerApp().mainloop()
