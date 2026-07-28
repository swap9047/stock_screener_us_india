import os
from dotenv import load_dotenv
load_dotenv('.env')

from google import genai
from google.genai import types

def generate():
    client = genai.Client()
    model = "models/gemma-4-31b-it"
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="What is 2+2?"),
            ],
        ),
    ]
    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(
            thinking_level="HIGH",
        ),
    )

    try:
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=generate_content_config,
        )
        print("Success! Response:", resp.text)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    generate()
