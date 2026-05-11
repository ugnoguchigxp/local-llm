import requests
import json
from typing import Generator, List, Dict, Any
from .base import BaseBackend

class OllamaBackend(BaseBackend):
    def __init__(self, host: str = "http://localhost:11434", verbose: bool = False):
        super().__init__(verbose=verbose)
        self.host = host
        self.model_name = None

    def load_model(self, model_name: str):
        if self.verbose:
            print(f"[Debug] Selecting Ollama model: {model_name}")
        self.model_name = model_name

    def generate_stream(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        if not self.model_name:
            raise ValueError("Model not selected. Call load_model() first.")

        url = f"{self.host}/api/chat"
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": kwargs.get("temperature", 0.0),
                "num_predict": kwargs.get("max_tokens", 1024),
            }
        }

        try:
            response = requests.post(url, json=payload, stream=True)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            print("\n[Error] Ollama server is not running. Please start Ollama first.")
            return
        except requests.exceptions.HTTPError as e:
            print(f"\n[Error] Ollama API error: {e}")
            return

        for line in response.iter_lines():
            if line:
                chunk = json.loads(line)
                if "message" in chunk:
                    yield chunk["message"].get("content", "")
                if chunk.get("done"):
                    break

    def list_models(self) -> List[str]:
        try:
            url = f"{self.host}/api/tags"
            response = requests.get(url, timeout=2)
            response.raise_for_status()
            data = response.json()
            return [m["name"] for m in data.get("models", [])]
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return []
