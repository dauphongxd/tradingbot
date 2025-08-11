import cv2
from ultralytics import YOLO
from PIL import Image
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
import torch

# === CONFIGURATION ===
image_path = "image6.jpg"
model_path = "runs/detect/train/weights/best.pt"

# === LOAD YOLO MODEL AND IMAGE ===
model = YOLO(model_path)
results = model.predict(source=image_path, save=False, imgsz=640)
image = cv2.imread(image_path)

# === LOAD TrOCR MODEL AND PROCESSOR ===
processor = TrOCRProcessor.from_pretrained("microsoft/trocr-large-printed")
trocr_model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-large-printed")
trocr_model.to("cuda")

# === PROCESS DETECTIONS ===
for result in results:
    boxes = result.boxes
    names = result.names  # class index to label

    for box in boxes:
        cls = int(box.cls[0])
        label = names[cls]  # 'entry', 'stoploss', 'target'
        x1, y1, x2, y2 = map(int, box.xyxy[0])

        # Optionally expand the box slightly
        expand = 3
        x1, y1 = x1 - expand, y1 - expand
        x2, y2 = x2 + expand, y2 + expand

        # Clamp within image bounds
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(image.shape[1], x2), min(image.shape[0], y2)

        # Crop and resize
        crop = image[y1:y2, x1:x2]
        resized = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        # Convert OpenCV (BGR) to PIL (RGB)
        pil_image = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))

        # Run TrOCR
        pixel_values = processor(images=pil_image, return_tensors="pt").pixel_values.to("cuda")  # Use GPU/CUDA instead
        with torch.no_grad():
            generated_ids = trocr_model.generate(pixel_values)
        text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]

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

        # Optional: Save for debugging
        cv2.imwrite(f"output_crop_{label}.jpg", resized)
