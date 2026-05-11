from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler
from typing import Generator, List, Dict
from .base import BaseBackend

DEFAULT_PREFILL_STEP_SIZE = 8192


class MLXBackend(BaseBackend):
    def __init__(
        self,
        verbose: bool = False,
        mtp_enabled: bool = False,
        draft_model_path: str | None = None,
        draft_kind: str = "mtp",
        draft_block_size: int = 6,
        prefill_step_size: int | None = DEFAULT_PREFILL_STEP_SIZE,
    ):
        super().__init__(verbose=verbose)
        self.model = None
        self.tokenizer = None
        self.model_path = None
        self.mtp_enabled = mtp_enabled
        self.uses_vlm = False
        self.draft_model_path = draft_model_path
        self.draft_kind = draft_kind
        self.draft_block_size = draft_block_size
        self.prefill_step_size = prefill_step_size
        self.draft_model = None

    def load_model(self, model_path: str):
        if self.verbose:
            print(f"[Debug] Loading MLX model: {model_path}...")

        self.uses_vlm = self.mtp_enabled or "gemma-4" in model_path.lower()
        if self.uses_vlm:
            from mlx_vlm.utils import load as load_vlm

            self.model, self.tokenizer = load_vlm(model_path)
            if self.mtp_enabled:
                if not self.draft_model_path:
                    raise ValueError("MTP is enabled but no draft model was configured.")
                from mlx_vlm.speculative.drafters import load_drafter

                self.draft_model, self.draft_kind = load_drafter(
                    self.draft_model_path,
                    kind=self.draft_kind,
                )
        else:
            self.model, self.tokenizer = load(model_path)

        self.model_path = model_path

    def generate_stream(self, messages: List[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        if not self.model:
            raise ValueError("Model not loaded. Call load_model() first.")

        # MLX用プロンプト作成
        prompt = self.tokenizer.apply_chat_template(
            messages, 
            add_generation_prompt=True, 
            tokenize=False
        )
        
        # パラメータ設定
        max_tokens = kwargs.get("max_tokens", 1024)
        temp = kwargs.get("temperature", 0.0)
        
        # 生成
        import time
        start_time = time.perf_counter()
        token_count = 0

        if self.uses_vlm:
            from mlx_vlm.generate import stream_generate as vlm_stream_generate

            generation_kwargs = {
                "max_tokens": max_tokens,
                "temperature": temp,
                "verbose": False,
                "prefill_step_size": self.prefill_step_size,
            }
            if self.mtp_enabled:
                if self.draft_model is None:
                    raise ValueError("MTP is enabled but the draft model is not loaded.")
                generation_kwargs.update(
                    {
                        "draft_model": self.draft_model,
                        "draft_kind": self.draft_kind,
                        "draft_block_size": self.draft_block_size,
                    }
                )

            for response in vlm_stream_generate(
                self.model,
                self.tokenizer,
                prompt=prompt,
                **generation_kwargs,
            ):
                chunk = response.text
                token_count += 1
                if chunk:
                    yield chunk
        else:
            for response in stream_generate(
                self.model,
                self.tokenizer,
                prompt=prompt,
                sampler=make_sampler(temp),
                max_tokens=max_tokens,
                prefill_step_size=self.prefill_step_size,
            ):
                chunk = response.text
                token_count += 1
                if chunk:
                    yield chunk

        end_time = time.perf_counter()
        duration = end_time - start_time
        if duration > 0 and self.verbose:
            tps = token_count / duration
            print(f"\n[MLX] Generated {token_count} chunks in {duration:.2f}s ({tps:.2f} chunks/sec)", flush=True)

    def list_models(self) -> List[str]:
        return []
