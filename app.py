from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import tempfile
import subprocess
import zipfile
from typing import Optional
import uvicorn
import shutil
import asyncio
from pathlib import Path

app = FastAPI(
    title="Spleeter Audio Separation API",
    description="API for separating audio tracks using Spleeter",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

ALLOWED_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg'}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB (reduced for Render free tier)

# Directory for storing temporary files
TEMP_DIR = Path(tempfile.gettempdir()) / "spleeter_api"
TEMP_DIR.mkdir(exist_ok=True)

def validate_audio_file(file: UploadFile):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    extension = file.filename.split('.')[-1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400, 
            detail=f"File type '{extension}' not allowed. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
        )

async def cleanup_file(file_path: str, delay: int = 60):
    """Clean up a file after a delay"""
    await asyncio.sleep(delay)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Cleaned up file: {file_path}")
        # Also clean up the parent directory if it's empty
        parent_dir = os.path.dirname(file_path)
        if os.path.exists(parent_dir) and not os.listdir(parent_dir):
            os.rmdir(parent_dir)
            print(f"Cleaned up empty directory: {parent_dir}")
    except Exception as e:
        print(f"Failed to cleanup {file_path}: {e}")

class CustomFileResponse(FileResponse):
    """FileResponse that schedules file cleanup after serving"""
    
    def __init__(self, *args, cleanup_path: str = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup_path = cleanup_path
    
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Schedule cleanup after response is sent
            if self.cleanup_path:
                asyncio.create_task(cleanup_file(self.cleanup_path))

@app.get("/")
async def root():
    return {
        "message": "Spleeter Audio Separation API",
        "docs": "/docs",
        "health": "/health",
        "status": "running"
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "Spleeter API"}

@app.post("/separate")
async def separate_audio(
    audio: UploadFile = File(...),
    stems: Optional[int] = Form(2)
):
    """
    Separate audio into stems
    
    - **audio**: Audio file to separate (mp3, wav, flac, m4a, ogg)
    - **stems**: Number of stems (2, 4, or 5)
    """
    
    # Validate inputs
    validate_audio_file(audio)
    
    if stems not in [2, 4, 5]:
        raise HTTPException(status_code=400, detail="Stems must be 2, 4, or 5")
    
    # Check file size
    contents = await audio.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 25MB)")
    
    # Create unique temporary directory that won't be auto-cleaned
    import uuid
    unique_id = str(uuid.uuid4())
    temp_dir = TEMP_DIR / unique_id
    temp_dir.mkdir(exist_ok=True)
    
    try:
        # Save uploaded file
        input_path = temp_dir / audio.filename
        with open(input_path, 'wb') as f:
            f.write(contents)
        
        print(f"Input file saved: {input_path}")
        print(f"File size: {input_path.stat().st_size} bytes")
        
        # Create output directory
        output_dir = temp_dir / 'output'
        output_dir.mkdir(exist_ok=True)
        
        # Run Spleeter with timeout for Render
        model = f"spleeter:{stems}stems-16kHz"
        cmd = [
            'spleeter', 'separate',
            '-p', model,
            '-o', str(output_dir),
            str(input_path)
        ]
        
        print(f"Running command: {' '.join(cmd)}")
        
        # Add timeout to prevent hanging on Render
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        print(f"Spleeter return code: {result.returncode}")
        print(f"Spleeter stdout: {result.stdout}")
        print(f"Spleeter stderr: {result.stderr}")
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Spleeter processing failed: {result.stderr}"
            )
        
        # Debug: List all files in output directory
        print(f"Contents of output directory {output_dir}:")
        for root, dirs, files in os.walk(output_dir):
            level = root.replace(str(output_dir), '').count(os.sep)
            indent = ' ' * 2 * level
            print(f"{indent}{Path(root).name}/")
            subindent = ' ' * 2 * (level + 1)
            for file in files:
                print(f"{subindent}{file}")
        
        # Create zip file with all tracks
        base_name = Path(audio.filename).stem
        track_dir = output_dir / base_name
        
        print(f"Looking for track directory: {track_dir}")
        print(f"Track directory exists: {track_dir.exists()}")
        
        if not track_dir.exists():
            # Try to find the actual output directory
            subdirs = [d for d in output_dir.iterdir() if d.is_dir()]
            if subdirs:
                track_dir = subdirs[0]
                print(f"Using alternative track directory: {track_dir}")
            else:
                raise HTTPException(status_code=500, detail=f"No output tracks found. Expected: {track_dir}")
        
        # Check if there are any audio files in the track directory
        audio_files = []
        for root, dirs, files in os.walk(track_dir):
            for file in files:
                if file.lower().endswith(('.wav', '.mp3', '.flac')):
                    audio_files.append(Path(root) / file)
        
        if not audio_files:
            raise HTTPException(status_code=500, detail="No audio files found in output directory")
        
        print(f"Found {len(audio_files)} audio files")
        
        zip_path = temp_dir / f'{base_name}_separated.zip'
        
        print(f"Creating zip file: {zip_path}")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(track_dir):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(track_dir)
                    zipf.write(file_path, arcname)
                    print(f"Added to zip: {arcname}")
        
        print(f"Zip file created: {zip_path.exists()}")
        print(f"Zip file size: {zip_path.stat().st_size if zip_path.exists() else 'N/A'}")
        
        if not zip_path.exists():
            raise HTTPException(status_code=500, detail="Failed to create zip file")
        
        return CustomFileResponse(
            str(zip_path),
            media_type='application/zip',
            filename=f'{base_name}_separated.zip',
            cleanup_path=str(temp_dir)  # Schedule cleanup of entire temp directory
        )
        
    except subprocess.TimeoutExpired:
        # Clean up on timeout
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise HTTPException(status_code=408, detail="Processing timeout - file may be too large")
    except HTTPException:
        # Clean up on HTTP exceptions
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    except Exception as e:
        # Clean up on other exceptions
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/models")
async def get_available_models():
    """Get list of available Spleeter models"""
    return {
        "models": [
            {
                "stems": 2,
                "name": "2stems-16kHz",
                "description": "Vocals and accompaniment"
            },
            {
                "stems": 4,
                "name": "4stems-16kHz", 
                "description": "Vocals, drums, bass, other"
            },
            {
                "stems": 5,
                "name": "5stems-16kHz",
                "description": "Vocals, drums, bass, piano, other"
            }
        ]
    }

# Cleanup task that runs periodically to remove old files
@app.on_event("startup")
async def startup_cleanup():
    """Clean up any leftover temporary files on startup"""
    try:
        if TEMP_DIR.exists():
            for item in TEMP_DIR.iterdir():
                if item.is_dir():
                    # Remove directories older than 1 hour
                    import time
                    if time.time() - item.stat().st_mtime > 3600:
                        shutil.rmtree(item)
                        print(f"Cleaned up old directory: {item}")
    except Exception as e:
        print(f"Startup cleanup failed: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)