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
import hmac
from config.settings import Config
from core.secret_safety import redact_text

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_server")

app = Flask(__name__)

# OTP Queue
otp_queue = Queue()

@app.before_request
def authenticate():
    configured = getattr(Config, "VOICE_SERVER_TOKEN", "")
    configured = configured.strip() if isinstance(configured, str) else ""
    # An absent or stock token must never turn authentication into an open
    # endpoint.  This check intentionally happens before reading request data.
    if not configured or configured.lower() == "changeme":
        return "Unauthorized", 401
    token = request.headers.get("X-Voice-Token") or request.args.get("token", "")
    if not isinstance(token, str):
        return "Unauthorized", 401
    try:
        matches = hmac.compare_digest(
            token.encode("utf-8"), configured.encode("utf-8")
        )
    except UnicodeError:
        matches = False
    if not matches:
        return "Unauthorized", 401

# Directories
TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route("/voice", methods=['POST'])
def receive_call():
    """
    Webhook for Twilio/Telnyx to send call recording.
    Expects 'RecordingUrl' in form data.
    """
    try:
        # Twilio sends RecordingUrl
        audio_url = request.form.get('RecordingUrl')
        if not audio_url:
            return "No recording URL", 400

        logger.info("[+] Received call recording: %s", redact_text(audio_url))
        
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
            logger.info("[+] Transcription completed (%d characters)", len(text or ""))
            
        # Extract 6-digit code
        clean_text = text.replace(" ", "")
        code_match = re.search(r'(\d{6})', clean_text)
        
        if code_match:
            code = code_match.group(1)
            logger.info("[+] OTP received and queued")
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
        logger.error("[-] Error processing call: %s", type(e).__name__)
        return "Voice processing failed", 500

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
    host = os.getenv("VOICE_SERVER_HOST", "").strip() or "127.0.0.1"
    serve(app, host=host, port=port, threads=4)

if __name__ == "__main__":
    run_server()
