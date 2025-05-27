from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
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
            
            # Add timeout to prevent hanging on Render
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Spleeter processing failed: {result.stderr}"
                )
            
            # Create zip file with all tracks
            base_name = os.path.splitext(audio.filename)[0]
            track_dir = os.path.join(output_dir, base_name)
            
            if not os.path.exists(track_dir):
                raise HTTPException(status_code=500, detail="No output tracks found")
            
            zip_path = os.path.join(temp_dir, f'{base_name}_separated.zip')
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(track_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, track_dir)
                        zipf.write(file_path, arcname)
            
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