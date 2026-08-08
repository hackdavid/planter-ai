"""Test Gemini 2.5 Flash Image generation with planter + venue composite."""

import os
from pathlib import Path
from PIL import Image
from dotenv import load_dotenv

load_dotenv(".env")

from google import genai
from google.genai import types

FRONTAGE_PATH = Path("planter_app/data/images/ChIJR6kvFRELdkgR7deUVt8nLws/streetview_primary_299.jpg")
PLANTER_PATH = Path("sample_plants/plant1.png")
OUTPUT_PATH = Path("test_gemini_output.jpg")

MODEL = "gemini-2.5-flash-image"

PROMPT = (
    "Place the potted plant from the first image onto the sidewalk in front of the storefront "
    "in the second image. The plant should sit naturally on the ground to the left of the entrance, "
    "with correct scale, soft shadow, and lighting that matches the scene. Keep the building, "
    "signage, and all existing objects completely unchanged."
)


def main():
    client = genai.Client(api_key=os.getenv("GOOGLE_GEMINI_API_KEY"))

    print(f"Loading images...")
    planter_img = Image.open(PLANTER_PATH)
    frontage_img = Image.open(FRONTAGE_PATH)

    print(f"Planter: {planter_img.size} | {planter_img.mode}")
    print(f"Frontage: {frontage_img.size} | {frontage_img.mode}")
    print(f"Model: {MODEL}")
    print(f"Prompt: {PROMPT[:100]}...")
    print("-" * 50)

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[
                PROMPT,
                planter_img,
                frontage_img,
            ],
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )

        print(f"Response received!")
        print(f"Text: {response.text if hasattr(response, 'text') else 'N/A'}")

        # Extract generated image
        for part in response.candidates[0].content.parts:
            if part.inline_data:
                data = part.inline_data.data
                mime = part.inline_data.mime_type
                print(f"Image part found: {mime}, {len(data)} bytes")
                OUTPUT_PATH.write_bytes(data)
                print(f"Saved to: {OUTPUT_PATH}")

                # Display the image
                gen_img = Image.open(OUTPUT_PATH)
                print(f"Generated image size: {gen_img.size} | mode: {gen_img.mode}")

    except Exception as exc:
        print(f"ERROR: {exc}")
        raise


if __name__ == "__main__":
    main()
