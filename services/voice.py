from flask import Flask, request
import speech_recognition as sr
from pydub import AudioSegment
import requests
import os
import re
import time
import threading
from queue import Queue
import logging
from config.settings import Config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_server")

app = Flask(__name__)

# OTP Queue
otp_queue = Queue()

# Directories
TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route("/voice", methods=['POST'])
def receive_call():
    """
    Webhook for Twilio/Telnyx to send call recording.
    Expects 'RecordingUrl' in form data.
    """
    # Security check (if configured)
    token = request.args.get('token')
    if Config.VOICE_SERVER_TOKEN and Config.VOICE_SERVER_TOKEN != "changeme":
        if token != Config.VOICE_SERVER_TOKEN:
             logger.warning("Unauthorized access attempt to voice server")
             return "Unauthorized", 401

    try:
        # Twilio sends RecordingUrl
        audio_url = request.form.get('RecordingUrl')
        if not audio_url:
            return "No recording URL", 400

        logger.info(f"[+] Received call recording: {audio_url}")
        
        # Download audio
        audio_response = requests.get(audio_url + ".mp3") # Twilio usually provides .mp3 extension
        if audio_response.status_code != 200:
            audio_response = requests.get(audio_url)
            
        local_mp3 = os.path.join(TEMP_DIR, f"call_{int(time.time())}.mp3")
        with open(local_mp3, "wb") as f:
            f.write(audio_response.content)
            
        # Convert to WAV for speech recognition
        local_wav = local_mp3.replace(".mp3", ".wav")
        sound = AudioSegment.from_mp3(local_mp3)
        sound.export(local_wav, format="wav")
        
        # Transcribe
        recognizer = sr.Recognizer()
        with sr.AudioFile(local_wav) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data)
            logger.info(f"[+] Transcription: {text}")
            
        # Extract 6-digit code
        clean_text = text.replace(" ", "")
        code_match = re.search(r'(\d{6})', clean_text)
        
        if code_match:
            code = code_match.group(1)
            logger.info(f"[+] OTP FLAGGED: {code}")
            otp_queue.put({"code": code, "timestamp": time.time()})
            
            # Cleanup
            try:
                os.remove(local_mp3)
                os.remove(local_wav)
            except:
                pass
                
            return "OK", 200
        else:
            logger.info("[-] No OTP found in audio")
            return "No OTP found", 200
            
    except Exception as e:
        logger.error(f"[-] Error processing call: {e}")
        return str(e), 500

@app.route("/otp", methods=['GET'])
def get_otp():
    """Endpoint for main script to poll for OTP"""
    if not otp_queue.empty():
        return otp_queue.get()
    return {"code": None}

def run_server():
    """Run the Flask server using waitress (production-grade WSGI server)"""
    from waitress import serve
    port = 5000
    logger.info(f"[*] Voice OTP Server listening on port {port} (waitress)")
    serve(app, host='0.0.0.0', port=port, threads=4)

if __name__ == "__main__":
    run_server()
