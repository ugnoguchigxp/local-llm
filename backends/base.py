from abc import ABC, abstractmethod
from typing import Generator, List, Dict, Any

class BaseBackend(ABC):
    """
    LLM推論エンジンの抽象基底クラス。
    """
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
    
    @abstractmethod
    def load_model(self, model_path: str):
        """モデルをロードする"""
        pass
        
    @abstractmethod
    def generate_stream(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        """
        メッセージ履歴を受け取り、ストリーミング形式でレスポンスを生成する。
        """
        pass
        
    @abstractmethod
    def list_models(self) -> List[str]:
        """利用可能なモデルの一覧を取得する"""
        pass
