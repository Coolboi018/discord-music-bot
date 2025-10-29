#!/usr/bin/env bash
# exit on error
set -o errexit

# Install FFmpeg with all audio codecs (required for high-quality audio processing)
apt-get update && apt-get install -y ffmpeg libavcodec-extra

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt