import cv2
from ultralytics import YOLO
import easyocr
import os

# === CONFIGURATION ===
image_path = "image3.jpg"
model_path = "runs/detect/train/weights/best.pt"

# === LOAD YOLO MODEL AND IMAGE ===
model = YOLO(model_path)
results = model.predict(source=image_path, save=False, imgsz=640)
image = cv2.imread(image_path)

# === LOAD EASYOCR READER ===
reader = easyocr.Reader(['en'], gpu=True)  # Set gpu=False if you don’t have CUDA

# === PROCESS DETECTIONS ===
for result in results:
    boxes = result.boxes
    names = result.names  # class index to label

    for box in boxes:
        cls = int(box.cls[0])
        label = names[cls]  # 'entry', 'stoploss', 'target'
        x1, y1, x2, y2 = map(int, box.xyxy[0])

        # Optionally shrink the box slightly to avoid overlap
        expand = 3  # You can increase this value as needed
        x1, y1 = x1 - expand, y1 - expand
        x2, y2 = x2 + expand, y2 + expand

        # Ensure crop bounds are within image
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(image.shape[1], x2), min(image.shape[0], y2)

        # Crop the detection box
        crop = image[y1:y2, x1:x2]

        # Optional: resize for better OCR performance
        resized = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        # Run OCR using EasyOCR
        results = reader.readtext(resized, detail=0)
        text = results[0] if results else ''

        # Clean OCR output
        cleaned = (
            text.strip()
            .replace('O', '0')
            .replace('B', '8')
            .replace('l', '1')
            .replace('I', '1')
            .replace(',', '.')
            .replace(' ', '')
        )

        print(f"{label.upper()}: {cleaned}")

        # Optional: Save cropped image for debugging
        cv2.imwrite(f"output_crop_{label}.jpg", resized)
