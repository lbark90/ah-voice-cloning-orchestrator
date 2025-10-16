"""
Voice Cloning Orchestrator for AliveHere
Cloud Run service that:
1. Checks if user has enough audio (≥15 seconds)
2. Calls pyglue to concatenate audio files
3. Sends concatenated audio to ElevenLabs for voice cloning
4. Saves voice_id to GCS
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from google.cloud import storage
from functools import wraps

app = Flask(__name__)

# Configuration
API_KEY = os.getenv('API_KEY')
GCP_BUCKET_NAME = os.getenv('GCP_BUCKET_NAME', 'memorial-voices')
PYGLUE_URL = os.getenv('PYGLUE_URL', 'https://pyglue-48354017394.us-central1.run.app')
PYGLUE_API_KEY = os.getenv('PYGLUE_API_KEY')
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
ELEVENLABS_API_URL = 'https://api.elevenlabs.io/v1'

# Initialize GCS client
storage_client = storage.Client()
bucket = storage_client.bucket(GCP_BUCKET_NAME)


def require_api_key(f):
    """Decorator to require API key authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided_key = request.headers.get('API_KEY') or request.headers.get('X-API-Key')
        if not provided_key or provided_key != API_KEY:
            return jsonify({'error': 'Unauthorized: Invalid API key'}), 401
        return f(*args, **kwargs)
    return decorated_function


def calculate_audio_duration(user_id):
    """
    Calculate total duration of all recordings for a user.
    Estimates based on file size: ~16KB per second for MP3 at 128kbps
    """
    print(f"Calculating audio duration for user: {user_id}")

    recordings_prefix = f"{user_id}/recordings/"
    blobs = list(bucket.list_blobs(prefix=recordings_prefix))

    audio_files = [b for b in blobs if b.name.endswith(('.mp3', '.wav', '.webm', '.m4a'))]

    total_bytes = sum(blob.size for blob in audio_files if blob.size)
    total_kb = total_bytes / 1024
    estimated_duration = total_kb / 16  # Rough estimate: 16KB per second

    print(f"Found {len(audio_files)} audio files, total size: {total_kb:.2f}KB, estimated duration: {estimated_duration:.1f}s")

    return {
        'duration_seconds': estimated_duration,
        'file_count': len(audio_files),
        'total_bytes': total_bytes
    }


def check_voice_exists(user_id):
    """Check if voice_id already exists for user"""
    voice_id_path = f"{user_id}/voice_id/voice_id.json"
    blob = bucket.blob(voice_id_path)

    try:
        exists = blob.exists()
        if exists:
            content = blob.download_as_text()
            data = json.loads(content)
            print(f"Voice ID already exists: {data.get('voice_id')}")
            return data.get('voice_id')
        return None
    except Exception as e:
        print(f"Error checking voice_id: {e}")
        return None


def call_pyglue(user_id):
    """Call pyglue service to concatenate audio files"""
    print(f"Calling pyglue to concatenate audio for user: {user_id}")

    url = f"{PYGLUE_URL}/webhook"
    headers = {
        'Content-Type': 'application/json',
        'API_KEY': PYGLUE_API_KEY,
        'X-API-Key': PYGLUE_API_KEY
    }
    payload = {'userId': user_id}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)

        if response.status_code == 200:
            # pyglue returns the MP3 file directly
            print(f"✓ Pyglue returned concatenated audio ({len(response.content)} bytes)")
            return response.content
        else:
            print(f"✗ Pyglue error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"✗ Exception calling pyglue: {e}")
        return None


def clone_voice_elevenlabs(user_id, audio_data, voice_name, voice_description):
    """Clone voice using ElevenLabs API"""
    print(f"Cloning voice on ElevenLabs: {voice_name}")

    url = f"{ELEVENLABS_API_URL}/voices/add"
    headers = {'xi-api-key': ELEVENLABS_API_KEY}

    # Prepare multipart form data
    files = {
        'files': (f'{user_id}_combined.mp3', audio_data, 'audio/mpeg')
    }
    data = {
        'name': voice_name,
        'description': voice_description
    }

    try:
        response = requests.post(url, headers=headers, data=data, files=files, timeout=60)

        if response.status_code == 200:
            result = response.json()
            voice_id = result.get('voice_id')
            print(f"✓ Voice cloned successfully! Voice ID: {voice_id}")
            return voice_id
        else:
            print(f"✗ ElevenLabs error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"✗ Exception calling ElevenLabs: {e}")
        return None


def save_voice_id(user_id, voice_id):
    """Save voice_id to GCS"""
    print(f"Saving voice_id to GCS for user: {user_id}")

    voice_id_data = {
        'userId': user_id,
        'voice_id': voice_id,
        'user_voice_id': voice_id,  # Backward compatibility
        'created_at': 'auto',
        'version': 1
    }

    blob_path = f"{user_id}/voice_id/voice_id.json"
    blob = bucket.blob(blob_path)

    try:
        blob.upload_from_string(
            json.dumps(voice_id_data, indent=2),
            content_type='application/json'
        )
        print(f"✓ Voice ID saved to gs://{GCP_BUCKET_NAME}/{blob_path}")
        return True
    except Exception as e:
        print(f"✗ Error saving voice_id: {e}")
        return False


def get_user_name(user_id):
    """Get user's first and last name from credentials"""
    creds_path = f"{user_id}/credentials/login_credentials.json"
    blob = bucket.blob(creds_path)

    try:
        if blob.exists():
            content = blob.download_as_text()
            data = json.loads(content)
            first_name = data.get('firstName', 'User')
            last_name = data.get('lastName', 'Voice')
            return first_name, last_name
    except Exception as e:
        print(f"Error getting user name: {e}")

    return 'User', 'Voice'


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'service': 'ah-voice-cloning-orchestrator'})


@app.route('/clone-voice', methods=['POST'])
@require_api_key
def clone_voice():
    """
    Main endpoint to orchestrate voice cloning

    Expected payload:
    {
        "userId": "string",
        "event": "recording_saved" | "voice_clone_requested",
        "recordingCount": number (optional),
        "questionIndex": number (optional)
    }
    """
    try:
        data = request.get_json()
        user_id = data.get('userId')

        if not user_id:
            return jsonify({'error': 'Missing userId'}), 400

        print(f"\n{'='*60}")
        print(f"Voice Cloning Request for User: {user_id}")
        print(f"{'='*60}")

        # Step 1: Check if voice already exists
        existing_voice = check_voice_exists(user_id)
        if existing_voice:
            print(f"Voice already exists for {user_id}, skipping clone")
            return jsonify({
                'success': True,
                'message': 'Voice already exists',
                'voice_id': existing_voice,
                'skipped': True
            })

        # Step 2: Calculate total audio duration
        audio_info = calculate_audio_duration(user_id)

        if audio_info['duration_seconds'] < 15:
            print(f"Insufficient audio: {audio_info['duration_seconds']:.1f}s < 15s required")
            return jsonify({
                'success': False,
                'error': 'Insufficient audio duration',
                'required': 15,
                'actual': round(audio_info['duration_seconds'], 1),
                'file_count': audio_info['file_count']
            }), 400

        print(f"✓ Sufficient audio: {audio_info['duration_seconds']:.1f}s")

        # Step 3: Call pyglue to concatenate audio
        concatenated_audio = call_pyglue(user_id)
        if not concatenated_audio:
            return jsonify({
                'success': False,
                'error': 'Failed to concatenate audio files'
            }), 500

        # Step 4: Get user name for voice description
        first_name, last_name = get_user_name(user_id)
        voice_name = f"{first_name} {last_name}"
        voice_description = f"AI voice clone for {voice_name}"

        # Step 5: Clone voice on ElevenLabs
        voice_id = clone_voice_elevenlabs(
            user_id,
            concatenated_audio,
            voice_name,
            voice_description
        )

        if not voice_id:
            return jsonify({
                'success': False,
                'error': 'Failed to clone voice on ElevenLabs'
            }), 500

        # Step 6: Save voice_id to GCS
        if not save_voice_id(user_id, voice_id):
            return jsonify({
                'success': False,
                'error': 'Failed to save voice_id to storage',
                'voice_id': voice_id  # Still return the ID even if save failed
            }), 500

        print(f"\n{'='*60}")
        print(f"✓ SUCCESS! Voice cloning complete")
        print(f"{'='*60}")
        print(f"User: {user_id}")
        print(f"Voice Name: {voice_name}")
        print(f"Voice ID: {voice_id}")
        print(f"Audio Duration: {audio_info['duration_seconds']:.1f}s")
        print(f"{'='*60}\n")

        return jsonify({
            'success': True,
            'message': 'Voice cloned successfully',
            'voice_id': voice_id,
            'user_id': user_id,
            'voice_name': voice_name,
            'audio_duration': round(audio_info['duration_seconds'], 1),
            'file_count': audio_info['file_count']
        })

    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with service info"""
    return jsonify({
        'service': 'AliveHere Voice Cloning Orchestrator',
        'version': '1.0',
        'endpoints': {
            '/health': 'GET - Health check',
            '/clone-voice': 'POST - Trigger voice cloning workflow'
        },
        'requirements': {
            'min_audio_duration': '15 seconds',
            'authentication': 'API_KEY or X-API-Key header required'
        }
    })


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
