#!/bin/bash
echo "Loading environment variables from .env..."
source .env
echo "Starting bot..."
python bot.py
