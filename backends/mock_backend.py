from typing import Generator, List, Dict, Any
from .base import BaseBackend

class MockBackend(BaseBackend):
    """
    テスト用のモックバックエンド。
    特定の入力に対して特定のタグを含むレスポンスを返す。
    """
    def __init__(self, verbose: bool = False):
        super().__init__(verbose=verbose)
        self.model_path = None

    def load_model(self, model_path: str):
        self.model_path = model_path
        if self.verbose:
            print(f"Mock model loaded: {model_path}")

    def generate_stream(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        last_user_message = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        
        # テストシナリオ
        if "test_tool" in last_user_message:
            # ツール呼び出しのシミュレーション
            yield "<think>Thinking about searching...</think>"
            yield "<|tool_call|>call:search_web{query:<|\"|>test query<|\"|>}<tool_call|>"
        elif "test_thinking" in last_user_message:
            # 思考タグのシミュレーション
            yield "<|channel>thought"
            yield "Wait, let me think..."
            yield "<channel|>"
            yield "I thought about it. Hello!"
        else:
            yield f"Mock response for: {last_user_message}"

    def list_models(self) -> List[str]:
        return ["mock-model-v1"]
