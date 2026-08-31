use std::{fs, path::Path};

use anyhow::{Context, Result, bail};
use fastembed::{
    InitOptionsUserDefined, OutputKey, Pooling, TextEmbedding, TokenizerFiles,
    UserDefinedEmbeddingModel,
};

pub struct EmbeddingEngine {
    model: TextEmbedding,
}

impl EmbeddingEngine {
    pub fn load(model_dir: &Path, intra_threads: usize) -> Result<Self> {
        let onnx_path = [
            model_dir.join("onnx/model.onnx"),
            model_dir.join("model.onnx"),
        ]
        .into_iter()
        .find(|path| path.is_file())
        .with_context(|| {
            format!(
                "ONNX model is missing below {} (expected onnx/model.onnx or model.onnx)",
                model_dir.display()
            )
        })?;
        let tokenizer_root = if model_dir.join("tokenizer.json").is_file() {
            model_dir
        } else if model_dir.join("onnx/tokenizer.json").is_file() {
            &model_dir.join("onnx")
        } else {
            bail!("tokenizer.json is missing below {}", model_dir.display());
        };
        let tokenizer_files = TokenizerFiles {
            tokenizer_file: read(tokenizer_root.join("tokenizer.json"))?,
            config_file: read(tokenizer_root.join("config.json"))?,
            special_tokens_map_file: read(tokenizer_root.join("special_tokens_map.json"))?,
            tokenizer_config_file: read(tokenizer_root.join("tokenizer_config.json"))?,
        };
        let user_model = UserDefinedEmbeddingModel::new(read(onnx_path)?, tokenizer_files)
            .with_pooling(Pooling::Mean);
        let options = InitOptionsUserDefined::default()
            .with_max_length(512)
            .with_intra_threads(intra_threads.max(1));
        let model = TextEmbedding::try_new_from_user_defined(user_model, options)
            .context("failed to initialize multilingual-e5 ONNX session")?;
        Ok(Self { model })
    }

    pub fn embed(&mut self, texts: &[String], normalize: bool) -> Result<Vec<Vec<f32>>> {
        if normalize {
            return self.model.embed(texts, Some(texts.len().max(1)));
        }
        let output = self.model.transform(texts, Some(texts.len().max(1)))?;
        output.export_with_transformer(|batches| {
            let precedence = [
                OutputKey::OnlyOne,
                OutputKey::ByName("text_embeds"),
                OutputKey::ByName("last_hidden_state"),
                OutputKey::ByName("sentence_embedding"),
            ];
            let mut embeddings = Vec::new();
            for batch in batches {
                let pooled =
                    batch.select_and_pool_output(&precedence.as_slice(), Some(Pooling::Mean))?;
                embeddings.extend(pooled.rows().into_iter().map(|row| row.to_vec()));
            }
            Ok(embeddings)
        })
    }
}

fn read(path: impl AsRef<Path>) -> Result<Vec<u8>> {
    let path = path.as_ref();
    fs::read(path).with_context(|| format!("failed to read model asset {}", path.display()))
}
