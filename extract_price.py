# extract_price.py

import cv2
from ultralytics import YOLO
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
import torch

# --- Load Models only once ---
# By loading them here, they stay in memory and don't need to be reloaded for every image.
print("Loading AI models (YOLO & TrOCR)...")
MODEL_PATH = "runs/detect/train/weights/best.pt"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

try:
    YOLO_MODEL = YOLO(MODEL_PATH)
    PROCESSOR = TrOCRProcessor.from_pretrained("microsoft/trocr-large-printed")
    TROCR_MODEL = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-large-printed").to(DEVICE)
    print("Models loaded successfully.")
except Exception as e:
    print(f"FATAL: Could not load AI models. Error: {e}")
    # Exit if models can't be loaded, as the bot can't function.
    exit()


def extract_prices_from_image(image_path: str) -> dict:
    """
    Takes an image path, runs YOLO and TrOCR, and returns a dictionary of cleaned prices.
    """
    try:
        image = cv2.imread(image_path)
        if image is None:
            print(f"Error: Could not read image at path: {image_path}")
            return {}

        results = YOLO_MODEL.predict(source=image_path, save=False, imgsz=640, verbose=False)

        extracted_data = {}

        for result in results:
            if result.boxes is None:
                continue

            for box in result.boxes:
                try:
                    cls = int(box.cls[0])
                    label = result.names[cls]  # 'entry', 'stoploss', 'target'

                    if label not in ['entry', 'stoploss', 'target']:
                        continue

                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    expand = 3
                    x1, y1 = max(0, x1 - expand), max(0, y1 - expand)
                    x2, y2 = min(image.shape[1], x2 + expand), min(image.shape[0], y2 + expand)

                    crop = image[y1:y2, x1:x2]
                    pil_image = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

                    pixel_values = PROCESSOR(images=pil_image, return_tensors="pt").pixel_values.to(DEVICE)
                    with torch.no_grad():
                        generated_ids = TROCR_MODEL.generate(pixel_values)

                    text = PROCESSOR.batch_decode(generated_ids, skip_special_tokens=True)[0]

                    cleaned = (
                        text.strip()
                        .replace('O', '0').replace('B', '8').replace('l', '1')
                        .replace('I', '1').replace(',', '').replace(' ', '')
                    )

                    extracted_data[label] = float(cleaned)
                except (ValueError, IndexError) as e:
                    print(f"Could not process a detection box for label '{label}'. Raw text: '{text}'. Error: {e}")
                    continue

        return extracted_data

    except Exception as e:
        print(f"An error occurred during price extraction: {e}")
        return {}