import json
import torch
from transformers import CLIPProcessor, CLIPModel
from PIL import Image
from rapidfuzz import fuzz
from datetime import datetime
import os

# -----------------------
# LOAD MODEL
# -----------------------
model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

# -----------------------
# LOAD DATA
# -----------------------
with open("images.json", "r") as f:
    data = json.load(f)

print("Total images loaded:", len(data))

# -----------------------
# USER INPUT
# -----------------------
user_query = input("Enter search query: ")
query = user_query + ", a photo of a person outdoors"

location_filter = input("Filter by location (optional): ").strip().lower()
date_input = input("Filter by date (optional): ").strip()
last_name_filter = input("Last name (optional): ").strip().lower()

# -----------------------
# DATE PARSING
# -----------------------
date_filter = ""
if date_input:
    for fmt in ["%m-%d-%y", "%m-%d-%Y", "%Y-%m-%d"]:
        try:
            parsed_date = datetime.strptime(date_input, fmt)
            date_filter = parsed_date.strftime("%m-%d-%y")
            break
        except:
            continue

# -----------------------
# TEXT → EMBEDDING
# -----------------------
inputs = processor(text=[query], return_tensors="pt", padding=True)

with torch.no_grad():
    text_features = model.get_text_features(**inputs)

text_embedding = text_features[0]

# -----------------------
# SEARCH
# -----------------------
results = []

for item in data:

    try:
        image_embedding = torch.tensor(item["embedding"])
        similarity = torch.cosine_similarity(text_embedding, image_embedding, dim=0).item()

        boost = 0

        # location boost
        if location_filter:
            score = fuzz.partial_ratio(location_filter, item["location"].lower())
            boost += (score / 100) * 0.3

        # date boost
        if date_filter and item["date"] == date_filter:
            boost += 0.2

        # last name filter (soft filter)
        if last_name_filter:
            score = fuzz.partial_ratio(last_name_filter, item.get("last_name", "").lower())
            if score < 80:
                continue

        final_score = similarity + boost

        # 🔥 IMPORTANT FIX (this was missing)
        results.append((final_score, item))

    except Exception as e:
        print("Error:", e)

# -----------------------
# SORT
# -----------------------
results.sort(reverse=True, key=lambda x: x[0])

print("Total results found:", len(results))

# -----------------------
# DISPLAY
# -----------------------
print("\nTop matches:\n")

top_results = results[:5]

for score, item in top_results:
    print(f"Score: {score:.4f}")
    print(f"Path: {item['path']}")

    try:
        date_obj = datetime.strptime(item['date'], "%m-%d-%y")
        formatted_date = date_obj.strftime("%B %d, %Y").replace(" 0", " ")
    except:
        formatted_date = item['date']

    print(f"Date: {formatted_date}")
    print(f"Location: {item['location']}")
    print("------")

# -----------------------
# OPEN IMAGES
# -----------------------
print("\nOpening top images...\n")

for score, item in top_results:
    path = item["path"]

    print("Trying to open:", path)

    if os.path.exists(path):
        try:
            img = Image.open(path)
            img.show()
        except:
            print(f"Could not open image: {path}")
    else:
        print(f"File not found: {path}")