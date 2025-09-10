from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import sqlite3
from datetime import datetime
import cv2
import pytesseract
import os
import threading
import time
import requests
from functools import wraps

app = Flask(__name__)
app.secret_key = 'kda_secret_key_2024'

# Tesseract OCR کا راستہ (Linux کے لیے)
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# لاگ ان کی ضرورت کے لیے decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ڈیٹا بیس کلاس
class VehicleDatabase:
    def __init__(self, db_path='kda_security.db'):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_plate TEXT UNIQUE NOT NULL,
                owner_name TEXT NOT NULL,
                address TEXT,
                phone_number TEXT,
                vehicle_type TEXT,
                registration_date DATE,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_name TEXT NOT NULL,
                camera_ip TEXT NOT NULL,
                camera_port INTEGER DEFAULT 554,
                camera_username TEXT,
                camera_password TEXT,
                rtsp_path TEXT DEFAULT "/stream1",
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_plate TEXT,
                image_path TEXT NOT NULL,
                capture_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                camera_id INTEGER,
                is_registered INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_vehicle(self, license_plate, owner_name, address=None, phone_number=None, vehicle_type=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO vehicles 
                (license_plate, owner_name, address, phone_number, vehicle_type, registration_date)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (license_plate, owner_name, address, phone_number, vehicle_type, datetime.now().date()))
            
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()
    
    def check_vehicle(self, license_plate):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM vehicles WHERE license_plate = ? AND is_active = 1', (license_plate,))
        vehicle = cursor.fetchone()
        conn.close()
        
        return vehicle is not None
    
    def add_camera(self, name, ip, port=554, username=None, password=None, rtsp_path="/stream1"):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO cameras 
                (camera_name, camera_ip, camera_port, camera_username, camera_password, rtsp_path)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, ip, port, username, password, rtsp_path))
            
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()
    
    def get_cameras(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM cameras WHERE is_active = 1')
        cameras = cursor.fetchall()
        conn.close()
        
        return cameras
    
    def get_camera_rtsp_url(self, camera_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT camera_ip, camera_port, camera_username, camera_password, rtsp_path 
            FROM cameras WHERE id = ?
        ''', (camera_id,))
        
        camera = cursor.fetchone()
        conn.close()
        
        if camera:
            ip, port, username, password, path = camera
            if username and password:
                return f"rtsp://{username}:{password}@{ip}:{port}{path}"
            else:
                return f"rtsp://{ip}:{port}{path}"
        return None
    
    def save_photo(self, license_plate, image_path, camera_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        is_registered = 1 if self.check_vehicle(license_plate) else 0
        
        cursor.execute('''
            INSERT INTO photos 
            (license_plate, image_path, camera_id, is_registered)
            VALUES (?, ?, ?, ?)
        ''', (license_plate, image_path, camera_id, is_registered))
        
        conn.commit()
        conn.close()
        
        return is_registered
    
    def search_photos(self, query_type, query_value, start_date=None, end_date=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if query_type == "license_plate":
            cursor.execute('''
                SELECT * FROM photos 
                WHERE license_plate LIKE ? 
                ORDER BY capture_time DESC
            ''', (f'%{query_value}%',))
        elif query_type == "date":
            cursor.execute('''
                SELECT * FROM photos 
                WHERE DATE(capture_time) = ?
                ORDER BY capture_time DESC
            ''', (query_value,))
        elif query_type == "datetime":
            if start_date and end_date:
                cursor.execute('''
                    SELECT * FROM photos 
                    WHERE capture_time BETWEEN ? AND ?
                    ORDER BY capture_time DESC
                ''', (start_date, end_date))
        
        results = cursor.fetchall()
        conn.close()
        
        return results

# ڈیٹا بیس آبجیکٹ
db = VehicleDatabase()

# NodeMCU کو کمانڈ بھیجنے کا فنکشن
def send_to_nodemcu(license_plate):
    try:
        # NodeMCU کا IP پتہ (اپنے نیٹ ورک کے مطابق تبدیل کریں)
        nodemcu_ip = "192.168.1.100"
        url = f"http://{nodemcu_ip}/relay"
        
        response = requests.post(url, json={
            "license_plate": license_plate,
            "command": "trigger"
        }, timeout=2)
        
        return response.status_code == 200
    except:
        return False

# کیمرہ فیڈ پروسیسنگ تھریڈ
def camera_processing_thread():
    while True:
        try:
            cameras = db.get_cameras()
            for camera in cameras:
                camera_id, name, ip, port, username, password, rtsp_path, active = camera
                rtsp_url = db.get_camera_rtsp_url(camera_id)
                
                if rtsp_url:
                    cap = cv2.VideoCapture(rtsp_url)
                    if cap.isOpened():
                        ret, frame = cap.read()
                        if ret:
                            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                            text = pytesseract.image_to_string(gray, config='--psm 8')
                            text = ''.join(e for e in text if e.isalnum()).upper()
                            
                            if len(text) >= 5:
                                print(f"کیمرہ {name} سے پہچانی گئی لائسنس پلیٹ: {text}")
                                
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                                image_path = f"static/photos/{text}_{timestamp}.jpg"
                                os.makedirs("static/photos", exist_ok=True)
                                cv2.imwrite(image_path, frame)
                                
                                is_registered = db.save_photo(text, image_path, camera_id)
                                
                                if is_registered:
                                    print(f"رجسٹرڈ گاڑی کا پتہ چلا: {text}")
                                    send_to_nodemcu(text)
                        
                        cap.release()
            
            time.sleep(5)
        except Exception as e:
            print(f"کیمرہ پروسیسنگ میں خرابی: {e}")
            time.sleep(10)

# Flask روٹس
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if username == 'telelenker' and password == 'kgf2929':
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='غلط صارف نام یا پاس ورڈ')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/cameras', methods=['GET', 'POST'])
@login_required
def cameras():
    if request.method == 'POST':
        name = request.form['name']
        ip = request.form['ip']
        port = request.form.get('port', 554)
        username = request.form.get('username')
        password = request.form.get('password')
        rtsp_path = request.form.get('rtsp_path', '/stream1')
        
        if db.add_camera(name, ip, port, username, password, rtsp_path):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'کیمرہ شامل نہیں کیا جا سکا'})
    
    cameras = db.get_cameras()
    return render_template('cameras.html', cameras=cameras)

@app.route('/vehicles', methods=['GET', 'POST'])
@login_required
def vehicles():
    if request.method == 'POST':
        license_plate = request.form['license_plate']
        owner_name = request.form['owner_name']
        address = request.form.get('address')
        phone_number = request.form.get('phone_number')
        vehicle_type = request.form.get('vehicle_type')
        
        if db.add_vehicle(license_plate, owner_name, address, phone_number, vehicle_type):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'گاڑی شامل نہیں کی جا سکی'})
    
    return render_template('vehicles.html')

@app.route('/search', methods=['GET', 'POST'])
@login_required
def search():
    if request.method == 'POST':
        query_type = request.form['query_type']
        query_value = request.form['query_value']
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        
        results = db.search_photos(query_type, query_value, start_date, end_date)
        return render_template('search_results.html', results=results)
    
    return render_template('search.html')

@app.route('/api/trigger_relay', methods=['POST'])
@login_required
def api_trigger_relay():
    data = request.json
    license_plate = data.get('license_plate')
    
    if license_plate and db.check_vehicle(license_plate):
        success = send_to_nodemcu(license_plate)
        return jsonify({'success': success})
    
    return jsonify({'success': False})

# کیمرہ پروسیسنگ تھریڈ شروع کریں
threading.Thread(target=camera_processing_thread, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)