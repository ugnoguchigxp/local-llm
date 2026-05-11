from .mlx import DEFAULT_PREFILL_STEP_SIZE, MLXBackend
from typing import Generator, List, Dict

class BonsaiBackend(MLXBackend):
    """
    Bonsai/Ternary-Bonsai models on the MLX backend.
    """

    def __init__(
        self,
        verbose: bool = False,
        prefill_step_size: int | None = DEFAULT_PREFILL_STEP_SIZE,
    ):
        super().__init__(
            verbose=verbose,
            mtp_enabled=False,
            prefill_step_size=prefill_step_size,
        )
    
    def load_model(self, model_path: str):
        if self.verbose:
            print(f"Loading Bonsai model: {model_path}...")
        try:
            super().load_model(model_path)
            if self.verbose:
                print("Successfully loaded model using MLX kernels.")
        except Exception as e:
            message = str(e)
            if "requested number of bits 1 is not supported" in message:
                print("Error: MLX 1-bit Bonsai requires the PrismML MLX fork.")
                print(
                    "Use the default Ternary-Bonsai MLX 2-bit model, or run with "
                    "a separate PrismML Bonsai environment for 1-bit models."
                )
            raise e

    def generate_stream(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        # Bonsaiのコンテキストウィンドウ設定（8k = 8192）
        # 必要に応じて、ここでメッセージ履歴のトークン数計算と切り詰めを行うロジックなどを追加可能
        kwargs.setdefault("max_tokens", 1024) 
        kwargs.setdefault("temperature", 0.0)
        
        if self.verbose:
            print(f"[Bonsai] Using 8k context window limit.")
            
        return super().generate_stream(messages, **kwargs)
