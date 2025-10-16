# AH Voice Cloning Orchestrator

Cloud Run service that orchestrates the voice cloning workflow for AliveHere.

## Features

- Checks if user has sufficient audio (≥15 seconds)
- Calls pyglue to concatenate audio files
- Sends to ElevenLabs for voice cloning
- Saves voice_id to GCS
- Prevents duplicate voice cloning
- RESTful API with authentication

## API Documentation

### Authentication

All requests must include an `API_KEY` or `X-API-Key` header.

### Endpoints

#### POST /clone-voice

Triggers the voice cloning workflow.

**Request:**
```json
{
  "userId": "string",
  "event": "recording_saved",
  "recordingCount": 3,
  "questionIndex": 2
}
```

**Response (Success):**
```json
{
  "success": true,
  "message": "Voice cloned successfully",
  "voice_id": "abc123...",
  "user_id": "username",
  "voice_name": "John Doe",
  "audio_duration": 18.5,
  "file_count": 3
}
```

**Response (Insufficient Audio):**
```json
{
  "success": false,
  "error": "Insufficient audio duration",
  "required": 15,
  "actual": 8.2,
  "file_count": 2
}
```

**Response (Already Exists):**
```json
{
  "success": true,
  "message": "Voice already exists",
  "voice_id": "existing_id",
  "skipped": true
}
```

#### GET /health

Health check endpoint.

## Workflow

1. **Check Existing** - Verifies if voice_id already exists
2. **Calculate Duration** - Sums all recording file sizes and estimates duration
3. **Validate Minimum** - Requires ≥15 seconds of audio
4. **Call PyGlue** - Concatenates all audio files into single MP3
5. **Clone Voice** - Sends to ElevenLabs API for voice cloning
6. **Save Result** - Stores voice_id in GCS

## Deployment

### Prerequisites

Create secrets in Google Cloud Secret Manager:

```bash
# Voice cloning service API key
echo -n "your-api-key-here" | gcloud secrets create ah-voice-clone-api-key --data-file=-

# PyGlue API key (get from existing pyglue service)
echo -n "pyglue-api-key" | gcloud secrets create pyglue-api-key --data-file=-

# ElevenLabs API key
echo -n "sk_536c1dd1a072eecebad234bbc5995354e8e260c3ea72187b" | gcloud secrets create elevenlabs-api-key --data-file=-
```

### Deploy to Cloud Run

```bash
cd cloud-run-services/ah-voice-cloning-orchestrator
gcloud builds submit
```

The `cloudbuild.yaml` handles:
- Building Docker image
- Pushing to Container Registry
- Deploying to Cloud Run with secrets

### Manual Deployment

```bash
# Build
gcloud builds submit --tag gcr.io/psyched-bee-455519-d7/ah-voice-cloning-orchestrator

# Deploy
gcloud run deploy ah-voice-cloning-orchestrator \
  --image gcr.io/psyched-bee-455519-d7/ah-voice-cloning-orchestrator \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --timeout 300s \
  --set-env-vars="GCP_BUCKET_NAME=memorial-voices,PYGLUE_URL=https://pyglue-48354017394.us-central1.run.app" \
  --update-secrets="API_KEY=ah-voice-clone-api-key:latest,PYGLUE_API_KEY=pyglue-api-key:latest,ELEVENLABS_API_KEY=elevenlabs-api-key:latest"
```

## Environment Variables

- `API_KEY` - Authentication key for this service (from Secret Manager)
- `GCP_BUCKET_NAME` - GCS bucket name (memorial-voices)
- `PYGLUE_URL` - PyGlue service URL
- `PYGLUE_API_KEY` - Authentication for PyGlue (from Secret Manager)
- `ELEVENLABS_API_KEY` - ElevenLabs API key (from Secret Manager)

## Integration with n8n

Use the provided n8n workflow JSON (`n8n-voice-cloning-workflow.json`) to trigger this service after each recording upload.

The workflow will:
1. Receive webhook from Next.js after recording upload
2. Call this Cloud Run service
3. Handle success/failure responses

## Local Testing

```bash
export API_KEY=test-key
export GCP_BUCKET_NAME=memorial-voices
export PYGLUE_URL=https://pyglue-48354017394.us-central1.run.app
export PYGLUE_API_KEY=<get from cloud run>
export ELEVENLABS_API_KEY=sk_536c1dd1a072eecebad234bbc5995354e8e260c3ea72187b
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json

pip install -r requirements.txt
python main.py

# Test
curl -X POST http://localhost:8080/clone-voice \
  -H "API_KEY: test-key" \
  -H "Content-Type: application/json" \
  -d '{"userId": "testuser", "event": "recording_saved"}'
```

## Monitoring

Check Cloud Run logs:
```bash
gcloud run services logs read ah-voice-cloning-orchestrator --region=us-central1
```

## Error Handling

The service returns appropriate HTTP status codes:
- `200` - Success
- `400` - Invalid request or insufficient audio
- `401` - Unauthorized (invalid API key)
- `500` - Server error (pyglue/ElevenLabs failure)
