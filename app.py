from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import tempfile
import subprocess
import zipfile
from typing import Optional
import uvicorn

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

def validate_audio_file(file: UploadFile):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    extension = file.filename.split('.')[-1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400, 
            detail=f"File type '{extension}' not allowed. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
        )

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
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save uploaded file
            input_path = os.path.join(temp_dir, audio.filename)
            with open(input_path, 'wb') as f:
                f.write(contents)
            
            print(f"Input file saved: {input_path}")
            print(f"File size: {os.path.getsize(input_path)} bytes")
            
            # Create output directory
            output_dir = os.path.join(temp_dir, 'output')
            os.makedirs(output_dir, exist_ok=True)
            
            # Run Spleeter with timeout for Render
            model = f"spleeter:{stems}stems-16kHz"
            cmd = [
                'spleeter', 'separate',
                '-p', model,
                '-o', output_dir,
                input_path
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
                level = root.replace(output_dir, '').count(os.sep)
                indent = ' ' * 2 * level
                print(f"{indent}{os.path.basename(root)}/")
                subindent = ' ' * 2 * (level + 1)
                for file in files:
                    print(f"{subindent}{file}")
            
            # Create zip file with all tracks
            base_name = os.path.splitext(audio.filename)[0]
            track_dir = os.path.join(output_dir, base_name)
            
            print(f"Looking for track directory: {track_dir}")
            print(f"Track directory exists: {os.path.exists(track_dir)}")
            
            if not os.path.exists(track_dir):
                # Try to find the actual output directory
                subdirs = [d for d in os.listdir(output_dir) if os.path.isdir(os.path.join(output_dir, d))]
                if subdirs:
                    track_dir = os.path.join(output_dir, subdirs[0])
                    print(f"Using alternative track directory: {track_dir}")
                else:
                    raise HTTPException(status_code=500, detail=f"No output tracks found. Expected: {track_dir}")
            
            # Check if there are any audio files in the track directory
            audio_files = []
            for root, dirs, files in os.walk(track_dir):
                for file in files:
                    if file.lower().endswith(('.wav', '.mp3', '.flac')):
                        audio_files.append(os.path.join(root, file))
            
            if not audio_files:
                raise HTTPException(status_code=500, detail="No audio files found in output directory")
            
            print(f"Found {len(audio_files)} audio files")
            
            zip_path = os.path.join(temp_dir, f'{base_name}_separated.zip')
            
            print(f"Creating zip file: {zip_path}")
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(track_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, track_dir)
                        zipf.write(file_path, arcname)
                        print(f"Added to zip: {arcname}")
            
            print(f"Zip file created: {os.path.exists(zip_path)}")
            print(f"Zip file size: {os.path.getsize(zip_path) if os.path.exists(zip_path) else 'N/A'}")
            
            if not os.path.exists(zip_path):
                raise HTTPException(status_code=500, detail="Failed to create zip file")
            
            return FileResponse(
                zip_path,
                media_type='application/zip',
                filename=f'{base_name}_separated.zip'
            )
            
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Processing timeout - file may be too large")
    except HTTPException:
        raise
    except Exception as e:
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)