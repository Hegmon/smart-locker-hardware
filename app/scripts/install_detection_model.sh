#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL_DIR="$PROJECT_DIR/app/streaming_agent/detection/models"
MODEL_PATH="$MODEL_DIR/detect.tflite"

# Use an SSD MobileNet V2 INT8 direct .tflite URL when available:
# PERSON_DETECTOR_MODEL_URL=https://.../detect.tflite app/scripts/install_detection_model.sh
MODEL_URL="${PERSON_DETECTOR_MODEL_URL:-}"

# This is a small official TensorFlow Lite COCO SSD MobileNet test model.
# It is useful to validate the full pipeline and GPIO LEDs, but it is not SSD MobileNet V2.
TEST_MODEL_URL="https://storage.googleapis.com/download.tensorflow.org/models/tflite/coco_ssd_mobilenet_v1_1.0_quant_2018_06_29.zip"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_PATH" ]; then
  echo "Model already exists: $MODEL_PATH"
  exit 0
fi

if [ -z "$MODEL_URL" ]; then
  echo "PERSON_DETECTOR_MODEL_URL is not set."
  echo "For production, set it to a direct SSD MobileNet V2 INT8 .tflite URL."
  echo "For pipeline testing only, downloading the official SSD MobileNet V1 quantized TFLite sample."
  MODEL_URL="$TEST_MODEL_URL"
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

echo "Downloading detection model from: $MODEL_URL"
case "$MODEL_URL" in
  *.zip)
    curl -L "$MODEL_URL" -o "$tmp_dir/model.zip"
    unzip -o "$tmp_dir/model.zip" -d "$tmp_dir/model"
    found_model="$(find "$tmp_dir/model" -type f \( -name 'detect.tflite' -o -name 'model.tflite' -o -name '*.tflite' \) | head -n 1)"
    if [ -z "$found_model" ]; then
      echo "No .tflite file found in downloaded zip" >&2
      exit 1
    fi
    cp "$found_model" "$MODEL_PATH"
    ;;
  *)
    curl -L "$MODEL_URL" -o "$MODEL_PATH"
    ;;
esac

if [ ! -s "$MODEL_PATH" ]; then
  echo "Downloaded model is empty: $MODEL_PATH" >&2
  exit 1
fi

echo "Detection model installed at: $MODEL_PATH"
