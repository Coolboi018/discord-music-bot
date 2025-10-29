#!/bin/bash

# Update package list
apt-get update

# Install FFmpeg (required for audio processing)
apt-get install -y ffmpeg

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt
